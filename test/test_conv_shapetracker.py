#!/usr/bin/env python
import unittest
from tinygrad.tensor import Tensor, Device
from tinygrad.nn import Conv2d
from tinygrad.ops import GlobalCounters
import pytest

pytestmark = pytest.mark.webgpu

#@unittest.skipUnless(Device.DEFAULT == "GPU", "Only GPU supports cache")
@unittest.skip("with JIT changes, you only get the raw buffer")
class TestConvShapetracker(unittest.TestCase):
  def test_conv_3x3_one_view(self):
    inp = Tensor.randn(1,16,10,10).realize()
    conv = Conv2d(16, 32, (3,3))
    conv(inp).realize()
    GlobalCounters.cache = []
    conv(inp).realize()
    test = GlobalCounters.cache
    GlobalCounters.cache = None
    assert len(test) == 1, f"conv should only have one kernel {[x[0].name for x in test]}"
    print(test[0][0].prg)
    for arg in test[0][1]:
      print(arg.st)
      assert len(arg.st.views) == 1

if __name__ == '__main__':
  unittest.main()
