import unittest

import torch
import torch.distributed.rpc as rpc
from torch.testing._internal.dist_utils import dist_init
from torch.testing._internal.distributed.rpc.rpc_agent_test_fixture import (
    RpcAgentTestFixture,
)


def rpc_return_rref(dst):
    return rpc.remote(dst, torch.add, args=(torch.ones(2, 2), 1))


@torch.jit.script
def one_arg(value):
    return value + 1


class MyScriptModuleWithRRefs(torch.jit.ScriptModule):
    def __init__(self, dst_worker):
        super().__init__()
        self.rrefs = []
        for _ in range(4):
            self.rrefs.append(rpc_return_rref(dst_worker))

    @torch.jit.script_method
    def forward(self):
        # type: () -> Tensor
        res_tensor = torch.ones(2, 2)
        for rref in self.rrefs:
            res_tensor += rref.to_here()

        return res_tensor


@torch.jit.script
class MyScriptClass:
    def __init__(self):
        self.a = 10


@torch.jit.interface
class MyModuleInterface(torch.nn.Module):
    def forward(self):
        # type: () -> Tensor
        pass


class MyScriptModule(torch.jit.ScriptModule):
    def __init__(self, rank):
        super().__init__()
        self.a = torch.ones(rank)

    @torch.jit.script_method
    def forward(self):
        # type: () -> Tensor
        return self.a


@torch.jit.script
def rref_to_here(rref_var):
    # type: (RRef[Tensor]) -> Tensor
    return rref_var.to_here()


@torch.jit.script
def return_rref(rref_var):
    # type: (RRef[Tensor]) -> RRef[Tensor]
    return rref_var


@torch.jit.ignore
def my_script_module_init(rank):
    # type: (int) -> MyModuleInterface
    return MyScriptModule(rank)


@torch.jit.script
def construct_my_script_module(rank):
    # type: (int) -> MyModuleInterface
    return my_script_module_init(rank)


@torch.jit.script
def run_ref_script_module(ref_script_module, t):
    # type: (RRef[MyModuleInterface], Tensor) -> Tensor
    module = ref_script_module.to_here()
    return module.forward() + t


@torch.jit.ignore
def rref_python_annotation(rref_var):
    # type: (RRef[Tensor]) -> RRef[Tensor]
    return rref_var


@torch.jit.script
def rref_script_annotation(rref_var):
    # type: (RRef[Tensor]) -> Tensor
    return rref_python_annotation(rref_var).to_here()


@unittest.skipIf(
    not torch._six.PY3, "Pytorch distributed rpc package does not support python2"
)
class JitRpcTest(RpcAgentTestFixture):
    @dist_init
    def test_torchscript_function(self):
        dst_worker_name = "worker{}".format((self.rank + 1) % self.world_size)
        local_ret = one_arg(torch.ones(2, 2))
        ret = rpc.rpc_sync(dst_worker_name, one_arg, args=(torch.ones(2, 2),))
        self.assertEqual(ret, local_ret)
        rref = rpc.remote(dst_worker_name, one_arg, args=(torch.ones(2, 2),))
        self.assertEqual(rref.to_here(), local_ret)
        # create rref to itself
        local_rref = rpc.remote(
            "worker{}".format(self.rank), one_arg, args=(torch.ones(2, 2),)
        )
        self.assertEqual(local_rref.to_here(), local_ret)

    @dist_init
    def test_torchscript_function_exception(self):
        dst_worker_name = "worker{}".format((self.rank + 1) % self.world_size)
        with self.assertRaisesRegex(RuntimeError, r"one_arg\(\) expected at most"):
            ret = rpc.rpc_sync(dst_worker_name, one_arg, args=(10, 20))

        with self.assertRaisesRegex(RuntimeError, r"one_arg\(\) expected at most"):
            rref = rpc.remote(dst_worker_name, one_arg, args=(10, 20))

    @dist_init
    def test_torchscript_functions_not_supported(self):
        # Right now _rpc_sync_torchscript does not accept annotated torchscript
        # class name or script module class name or their class method names.
        # But rpc_sync still accepts script class name and run it in
        # the same code path as python call.
        # Currently neither rpc_sync or _rpc_sync_torchscript is allowed to
        # accept script module and script module method.
        n = self.rank + 1
        dst_rank = n % self.world_size
        with self.assertRaisesRegex(
            RuntimeError, "attempted to get undefined function"
        ):
            ret = rpc._rpc_sync_torchscript(
                "worker{}".format(dst_rank),
                torch.jit._qualified_name(MyScriptClass),
                args=(),
            )
        ret = rpc.rpc_sync("worker{}".format(dst_rank), MyScriptClass, args=())

        with self.assertRaisesRegex(
            RuntimeError, "attempted to get undefined function"
        ):
            ret = rpc._rpc_sync_torchscript(
                "worker{}".format(dst_rank),
                torch.jit._qualified_name(MyScriptModule),
                args=(self.rank,),
            )

        with self.assertRaisesRegex(
            RuntimeError, "attempted to get undefined function"
        ):
            ret = rpc._rpc_sync_torchscript(
                "worker{}".format(dst_rank),
                torch.jit._qualified_name(MyScriptModule(self.rank).forward),
                args=(),
            )
        # Python 3.5 and Python 3.6 throw different error message, the only
        # common word can be greped is "pickle".
        with self.assertRaisesRegex(Exception, "pickle"):
            ret = rpc.rpc_sync(
                "worker{}".format(dst_rank), MyScriptModule(self.rank).forward, args=()
            )

    @dist_init
    def test_rref_as_arg_and_return(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        local_ret = one_arg(torch.ones(2, 2))

        # create rref on current rank
        rref = rpc.remote(
            "worker{}".format(self.rank), one_arg, args=(torch.ones(2, 2),)
        )

        # pass rref to another user in rpc call
        ret = rpc.rpc_sync("worker{}".format(dst_rank), rref_to_here, args=(rref,))
        self.assertEqual(ret, local_ret)

        # return rref in rpc call
        rref1 = rpc.rpc_sync("worker{}".format(dst_rank), return_rref, args=(rref,))
        self.assertEqual(rref1.to_here(), local_ret)

        # pass rref to another user in remote call
        rref2 = rpc.remote("worker{}".format(dst_rank), rref_to_here, args=(rref,))
        self.assertEqual(rref2.to_here(), local_ret)

        # return rref in remote call
        rref3 = rpc.remote("worker{}".format(dst_rank), return_rref, args=(rref,))
        self.assertEqual(rref3.to_here().to_here(), local_ret)

    @dist_init
    def test_remote_script_module(self):
        # TODO, need more investigation
        # there is rref leak when shutting down, suspect it is because
        # ref as arg is passed to pybind boundary, and the ref is not garbage
        # collected by python when calling shutdown()
        import torch.distributed.rpc.api as api

        api._ignore_rref_leak = True

        local_ret = MyScriptModule(self.rank).forward() + torch.ones(self.rank)

        n = self.rank + 1
        dst_rank = n % self.world_size
        remote_ref = rpc.remote(
            "worker{}".format(dst_rank), construct_my_script_module, args=(self.rank,)
        )

        # pass rref arg to owner
        ret = rpc.rpc_sync(
            "worker{}".format(dst_rank),
            run_ref_script_module,
            args=(remote_ref, torch.ones(self.rank)),
        )
        self.assertEqual(ret, local_ret)

    @dist_init
    def test_rref_is_owner(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref_var = rpc_return_rref("worker{}".format(dst_rank))

        @torch.jit.script
        def rref_tensor_is_owner(rref_var):
            # type: (RRef[Tensor]) -> bool
            return rref_var.is_owner()

        res = rref_tensor_is_owner(rref_var)
        self.assertEqual(res, False)

    @dist_init
    def test_my_script_module_with_rrefs(self):
        n = self.rank + 1
        dst_rank = n % self.world_size

        module_with_rrefs = MyScriptModuleWithRRefs("worker{}".format(dst_rank))
        res = module_with_rrefs()
        self.assertEqual(res, torch.ones(2, 2) * 9)

    @dist_init
    def test_rref_python_annotation(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref_var = rpc_return_rref("worker{}".format(dst_rank))

        res = rref_script_annotation(rref_var)
        self.assertEqual(res, torch.ones(2, 2) + 1)

    @dist_init
    def test_local_rref_creation_with_ivalue(self):

        # create a local RRef that holds a IValue
        rref_local_script_class = rpc.RRef(MyScriptClass())
        self.assertEqual(rref_local_script_class.to_here().a, 10)

        # create a local RRef that holds a ScriptModule
        rref_local_script_mod = rpc.RRef(MyScriptModule(3)._c)
        self.assertEqual(rref_local_script_mod.to_here().forward(), torch.ones(3))
