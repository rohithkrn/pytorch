import os
import sys

import torch

# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)
from torch.testing._internal.jit_utils import JitTestCase, warmup_backward, FileCheck

if __name__ == '__main__':
    raise RuntimeError("This test file is not meant to be run directly, use:\n\n"
                       "\tpython test/test_jit.py TESTNAME\n\n"
                       "instead.")

class TestProfiler(JitTestCase):
    def setUp(self):
        self.prev_exec = torch._C._jit_set_profiling_executor(True)
        self.prev_profiling = torch._C._jit_set_profiling_mode(True)
        self.inline_autodiff = torch._C._debug_set_autodiff_subgraph_inlining(False)
        self.texpr_fuser_state = torch._C._jit_texpr_fuser_enabled()
        torch._C._jit_set_texpr_fuser_enabled(True)


    def tearDown(self):
        torch._C._jit_set_profiling_executor(self.prev_exec)
        torch._C._jit_set_profiling_mode(self.prev_profiling)
        torch._C._debug_set_autodiff_subgraph_inlining(self.inline_autodiff)
        torch._C._jit_set_texpr_fuser_enabled(self.texpr_fuser_state)

    def test_specialize_backward(self):
        def test_fuse(a, b):
            c = a * b
            d = c * b
            return d

        test_fuse.__disable_jit_function_caching__ = True

        scripted_f = torch.jit.script(test_fuse)
        x = torch.ones(1, requires_grad=True)
        y = torch.ones(1, requires_grad=True)
        scripted_f(x, y)
        b = scripted_f(x, y)
        warmup_backward(b)
        g = torch.jit.last_executed_optimized_graph()
        # Backward has an if node guarding specializations,
        # within the if node true block there is only one if node
        # that guards a tensorexpr group
        optimized_block = next(g.findNode("prim::If").blocks())
        if_nodes = list(optimized_block.findAllNodes("prim::If"))
        self.assertEqual(len(if_nodes), 1)
        FileCheck().check("Group[Subgraph").run(str(if_nodes[0]))
        # no broadcasts occurred, sum_to_size have been specialized out
        self.assertIsNone(optimized_block.findNode("aten::_grad_sum_to_size"))

        broadcast_f = torch.jit.script(test_fuse)
        x = torch.ones([2, 2], requires_grad=True)
        y = torch.ones([1], requires_grad=True)
        broadcast_f(x, y)
        b = broadcast_f(x, y)
        b.backward(torch.ones([2, 2], dtype=torch.float))
        b.backward(torch.ones([2, 2], dtype=torch.float))
        # warmup_backward(b, torch.ones([2, 2], dtype=torch.float))
        g = torch.jit.last_executed_optimized_graph()
        optimized_block = next(g.findNode("prim::If").blocks())
        # broadcasts occurred, currently expect to see aten::_grad_sum_to_size
        self.assertIsNotNone(optimized_block.findNode("aten::_grad_sum_to_size"))
