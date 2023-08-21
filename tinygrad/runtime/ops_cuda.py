import subprocess, time, re, hashlib, tempfile, os, functools
from typing import Optional
import numpy as np
from tinygrad.helpers import DEBUG, getenv, colored
from tinygrad.ops import Compiled
from tinygrad.runtime.lib import RawBufferCopyInOut, RawMallocBuffer
from tinygrad.codegen.linearizer import LinearizerOptions
from tinygrad.renderer.cstyle import uops_to_cstyle, CStyleLanguage
from functools import lru_cache

def pretty_ptx(s):
  # all expressions match `<valid_before><expr><valid_after>` and replace it with `<valid_before>color(<expr>)<valid_after>`
  s = re.sub(r'([!@<\[\s,\+\-;\n])((?:[_%$][\w%\$_]+(?:\.[xyz])?\:?)|(?:buf\d+))([<>\]\s,\+\-;\n\)])', lambda m:m[1]+colored(m[2], "blue")+m[3], s, flags=re.M) # identifiers
  s = re.sub(r'(.)((?:b|s|u|f)(?:8|16|32|64)|pred)([\.\s])', lambda m:m[1]+colored(m[2], "green")+m[3], s, flags=re.M) # types
  s = re.sub(r'^(\s*)([\w]+)(.*?;$)', lambda m:m[1]+colored(m[2], "yellow")+m[3], s, flags=re.M) # instructions
  s = re.sub(r'([<>\[\]\s,\+\-;])((?:0[fF][0-9a-fA-F]{8})|(?:[0-9]+)|(?:0[xX][0-9a-fA-F]+))([<>\[\]\s,\+\-;])', lambda m:m[1]+colored(m[2], "yellow")+m[3], s, flags=re.M) # numbers
  s = re.sub(r'(\.)(param|reg|global)', lambda m:m[1]+colored(m[2], "magenta"), s, flags=re.M) # space
  s = re.sub(r'(\.)(version|target|address_size|visible|entry)', lambda m:m[1]+colored(m[2], "magenta"), s, flags=re.M) # derivatives
  return s
def arch(): return "sm_" + "".join([str(x) for x in pycuda.driver.Context.get_device().compute_capability()])

if getenv("CUDACPU", 0) == 1:
  import ctypes, ctypes.util
  lib = ctypes.CDLL(ctypes.util.find_library("gpuocelot"))
  lib.ptx_run.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
  class cuda:
    class module:
      def __init__(self, src): self.src = src
      def get_function(self, _): return self
      def __call__(self, *args, block, grid): lib.ptx_run(self.src, len(args), (ctypes.c_void_p * len(args))(*[ctypes.cast(x, ctypes.c_void_p) for x in args]), *block, *grid)
    module_from_buffer = lambda src: cuda.module(src) # pylint: disable=unnecessary-lambda # noqa: E731
    class Event:
      def __init__(self): pass
      def record(self): self.start = time.perf_counter()
      def time_till(self, other): return self.start - other.start
      def synchronize(self): pass
    class Context:
      synchronize = lambda:0 # noqa: E731
    CompileError = Exception
  class context:
    class device:
      compute_capability = lambda: (3,5) # pylint: disable=unnecessary-lambda # noqa: E731
    get_device = lambda: context.device # pylint: disable=unnecessary-lambda # noqa: E731
  import pycuda.driver # type: ignore
  pycuda.driver.Context = context
  RawCUDABuffer = RawMallocBuffer
else:
  import pycuda.autoprimaryctx # type: ignore # pylint: disable=unused-import # noqa: F401
  import pycuda.driver as cuda # type: ignore
  class RawCUDABuffer(RawBufferCopyInOut): # type: ignore
    def __init__(self, size, dtype): super().__init__(size, dtype, cuda.mem_alloc(size * dtype.itemsize)) # type: ignore
    def _copyin(self, x:np.ndarray, stream:Optional[cuda.Stream]=None): cuda.memcpy_htod_async(self._buf, x.ravel(), stream) # type: ignore
    def _copyout(self, x:np.ndarray): cuda.memcpy_dtoh(x, self._buf) # type: ignore

@lru_cache
def find_cicc_path():
  nvcc_path = subprocess.check_output(f"{'which' if os.name != 'nt' else 'where'} nvcc", shell=True).decode().split()[0]
  
  if os.path.getsize(nvcc_path) < 200: # get path from bin alias
    nvcc_dir = open(nvcc_path, "r").read().split('\n')[2][:-4]
  else:
    nvcc_dir = nvcc_path[:-4]

  tmp = nvcc_dir + "cicc"
  if os.path.exists(tmp): return tmp
  tmp = nvcc_dir + "../nvvm/bin/cicc"
  if os.path.exists(tmp): return tmp

  raise Exception("cicc not found")

CUDA_PROGRAM_HEADER = 'struct __attribute__((device_builtin)) uint3{unsigned int x, y, z;};struct __attribute__((device_builtin)) dim3{unsigned int x, y, z;__attribute__((host)) __attribute__((device)) constexpr operator uint3(void) const {return uint3{x,y,z};}};uint3 __attribute__((device_builtin)) extern const threadIdx;uint3 __attribute__((device_builtin)) extern const blockIdx;struct __attribute__((device_builtin)) __attribute__((aligned(16))) float4{float x, y, z, w;};struct __attribute__((device_builtin)) __attribute__((aligned(8))) float2 { float x; float y; };static __inline__ __attribute__((device)) float4 make_float4(float x, float y, float z, float w){float4 t; t.x = x; t.y = y; t.z = z; t.w = w; return t;}extern __attribute__((host)) __attribute__((device)) __attribute__((device_builtin)) float fmaxf(float x, float y) noexcept (true);static inline __attribute__((host)) __attribute__((device)) float max(const float a, const float b){return fmaxf(a, b);}extern __attribute__((host)) __attribute__((device)) __attribute__((device_builtin)) double sqrt(double x) noexcept (true);extern __attribute__((host)) __attribute__((device)) __attribute__((device_builtin)) double log2(double x) noexcept (true);extern __attribute__((host)) __attribute__((device)) __attribute__((device_builtin)) double exp2(double x) noexcept (true);extern __attribute__((host)) __attribute__((device)) __attribute__((device_builtin)) double sin(double x) noexcept (true);const float INFINITY = __builtin_inff(); const float NAN = __builtin_nanf ("");'

class CUDAProgram:
  def __init__(self, name:str, prg:str, binary=False):
    if not binary:
      fn = os.path.join(tempfile.gettempdir(), f"tinycuda_{hashlib.md5(prg.encode('utf-8')).hexdigest()}.ii")
      try: 
        if not os.path.exists(fn):
          with open(fn, 'w+') as f:
            f.write(CUDA_PROGRAM_HEADER + prg);f.flush()
            subprocess.run([find_cicc_path(),"-arch",f"compute_{arch()[3:]}","--allow_managed", "-m64","-ftz=0", "-prec_div=1", "-prec_sqrt=1", "-fmad=1", "-tused", fn, "-o",fn], check=True, stderr=subprocess.DEVNULL if DEBUG < 3 else None)
            f.seek(0);prg = f.read()
        else: # load cached
          with open(fn, 'r') as f: prg = f.read()
      except Exception as e:
        if DEBUG >= 3: print("FAILED TO BUILD", prg)
        os.remove(fn)
        raise e
    if DEBUG >= 5: print(pretty_ptx(prg))
    if DEBUG >= 6:
      try:
        fn = os.path.join(tempfile.gettempdir(), f"tinycuda_{hashlib.md5(prg.encode('utf-8')).hexdigest()}")
        with open(fn + ".ptx", "wb") as f: f.write(prg.encode('utf-8'))
        subprocess.run(["ptxas", f"-arch={arch()}", "-o", fn, fn+".ptx"], check=True)
        print(subprocess.check_output(['nvdisasm', fn]).decode('utf-8'))
      except Exception as e: print("failed to generate SASS", str(e))
    # TODO: name is wrong, so we get it from the ptx using hacks
    self.prg = cuda.module_from_buffer(prg.encode('utf-8')).get_function(prg.split(".visible .entry ")[1].split("(")[0])

  def __call__(self, global_size, local_size, *args, wait=False):
    if wait:
      start, end = cuda.Event(), cuda.Event()
      start.record()
    self.prg(*[x._buf for x in args], block=tuple(local_size), grid=tuple(global_size))
    if wait:
      end.record()
      end.synchronize()
      return start.time_till(end)*1e-3

renderer = functools.partial(uops_to_cstyle, CStyleLanguage(
  kernel_prefix = "__attribute__((global))", smem_prefix = "__attribute__((shared)) ", barrier = "__syncthreads();", float4 = "make_float4",
  gid = [f'blockIdx.{chr(120+i)}' for i in range(3)],
  lid = [f'threadIdx.{chr(120+i)}' for i in range(3)],
  half_prekernel = """
    #include <cuda_fp16.h>
    struct __align__(8) half4 {
      half2 x, y;
      __device__ __forceinline__ explicit half4(const float4& a): x(make_half2(__float2half(a.x), __float2half(a.y))), y(make_half2(__float2half(a.z),__float2half(a.w))) {}
      __device__ __forceinline__ explicit operator float4() const {return make_float4(__half2float(x.x), __half2float(x.y), __half2float(y.x), __half2float(y.y)); }
    };
  """))
CUDABuffer = Compiled(RawCUDABuffer, LinearizerOptions(supports_float4_alu=False, global_max = [65535, 65535, 2147483647], local_max = [64, 1024, 1024]), renderer, CUDAProgram, cuda.Context.synchronize)
