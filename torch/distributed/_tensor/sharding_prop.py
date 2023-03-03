from typing import Callable, Dict, Optional, Tuple

import torch
import torch.distributed._tensor.api as dtensor
from torch._ops import OpOverload
from torch._subclasses import FakeTensorMode
from torch.distributed._tensor.op_schema import DTensorSpec, OpSchema, OutputSharding
from torch.fx import Node
from torch.fx.experimental.proxy_tensor import get_isolated_graphmodule
from torch.utils._pytree import tree_map, tree_map_only, tree_flatten

"""
Print information on ops input shape and sharding for debugging purposes.
"""
_DEBUG_VERBOSE = False


def unwrap_spec(e: "dtensor.DTensor") -> DTensorSpec:
    return e._spec

def unwrap_spec_from_node(n: object) -> DTensorSpec:
    spec = n.meta["sharding"]
    spec.tensor_meta = n.meta["tensor_meta"]
    return spec

class ShardingPropagator(object):
    def __init__(self) -> None:
        self.op_to_rules: Dict[OpOverload, Callable[[OpSchema], OutputSharding]] = {}

    def register_sharding_prop_rule(
        self, op_overload: OpOverload, rule_func: Callable[[OpSchema], OutputSharding]
    ):
        """
        Register a sharding propagation rule for an operator.
        """
        self.op_to_rules[op_overload] = rule_func

    def _rebuild_tensor_from_dtensor(self, arg) -> object:
        """ "
        This is used to propagate sharding and tensor metadata, must be under fake mode
        """
        assert isinstance(arg, DTensorSpec), "must be DTensorSpec"
        tensor_meta = arg.tensor_meta
        assert tensor_meta is not None
        return torch.empty_strided(
            tensor_meta.shape,
            tensor_meta.stride(),
            dtype=tensor_meta.dtype,
            requires_grad=tensor_meta.requires_grad
        )

    def prepare_op_schema(
        self, op_call: OpOverload, args: Tuple[object, ...], kwargs: Dict[str, object]
    ) -> OpSchema:
        """
        This unwrap the args/kwargs DTensor to DTensorSpec and pack them
        into an OpSchema for sharding propagation usage.
        """
        args_schema = tree_map_only(dtensor.DTensor, unwrap_spec, args)
        kwargs_schema = tree_map_only(dtensor.DTensor, unwrap_spec, kwargs)

        op_schema = OpSchema(op_call._schema, args_schema, kwargs_schema)

        return op_schema

    def propagate(self, op_call: OpOverload, op_schema: OpSchema) -> OutputSharding:
        """
        Propagate the sharding for an operator given the args/kwargs.
        """
        args_schema = op_schema.args_schema
        kwargs_schema = op_schema.kwargs_schema

        # prepare a fake graph to run the propagation
        with FakeTensorMode():
            fake_args = tree_map_only(DTensorSpec, self._rebuild_tensor_from_dtensor, args_schema)
            fake_kwargs = tree_map_only(DTensorSpec, self._rebuild_tensor_from_dtensor, kwargs_schema)
            op_graph = get_isolated_graphmodule(op_call, fake_args, fake_kwargs)

        # unwrap the args/kwargs DTensor to DTensorSpec and pack them
        # into an OpSchema for sharding propagation usage. 
        # args_schema = tree_map_only(dtensor.DTensor, unwrap_spec, args)
        # kwargs_schema = tree_map_only(dtensor.DTensor, unwrap_spec, kwargs)


        if _DEBUG_VERBOSE and torch.distributed.get_rank() == 0:
            print(f"OpSchema({op_schema.func_schema})")
            local_shapes = tree_map(
                lambda t: t.to_local().shape
                if isinstance(t, dtensor.DTensor)
                else None,
                args,
            )
            print(f"    local shapes: {local_shapes}")

        # flatten the args schema/kwarg schema to feed into the graph
        flat_args_sharding, _ = tree_flatten([args_schema, kwargs_schema])

        return self.run_graph_prop(op_graph, flat_args_sharding)


        # return self.propagate_op_sharding(op_call, op_schema)

    def run_graph_prop(self, op_graph: torch.fx.Graph, flat_args_sharding):
        """
        Run the sharding propagation on the op_graph.
        """
        # NOTE: we assume the first few nodes are all placeholders
        placeholder_idx = 0
        output_sharding = None
        for node in op_graph.graph.nodes:
            if node.op == "placeholder":
                # set sharding to placeholders if it's Node
                if isinstance(flat_args_sharding[placeholder_idx], DTensorSpec):
                    print(f">>>> setting up sharding for node: {node}")
                    node.meta["sharding"] = flat_args_sharding[placeholder_idx]
                placeholder_idx += 1
            elif node.op == "call_function":
                output_sharding = self.run_op_prop(node)
            elif node.op == "output":
                # get the sharding from the output node
                output_nodes = node.all_input_nodes
                output_spec = tree_map_only(Node, unwrap_spec_from_node, output_nodes)
                # assert isinstance(output_nodes, (tuple, list))
                # for i, node in enumerate(output_nodes):
                #     if isinstance(node, Node):
                #         output_spec = node.meta["sharding"]
                        
                #         output_spec.tensor_meta = node.meta["tensor_meta"]


                # output_sharding = node.args[0]meta["sharding"]
                # # associate the output sharding with the output metadata
                # if output_shrding.output_spec is not None:
                #     if not isinstance(output_spec, (tuple, list)):
                #         output_sharding.output_spec = (output_spec,)

                    
                #     for i, spec in enumerate(output_spec):
                #         if isinstance(spec, DTensorSpec):
                #             spec.tensor_meta = output_nodes[i].meta["tensor_meta"]
            else:
                raise ValueError(f"Can't propagate sharding on node type: {node.op}")

        return OutputSharding(output_spec, schema_suggestions=output_sharding.schema_suggestions)

    # def prepare_op_schema(
    #     self, op_call: OpOverload, args: Tuple[object, ...], kwargs: Dict[str, object]
    # ) -> OpSchema:
    #     """
    #     This unwrap the args/kwargs DTensor to DTensorSpec and pack them
    #     into an OpSchema for sharding propagation usage.
    #     """
    #     args_schema = tree_map(unwrap_schema, args)
    #     kwargs_schema = tree_map(unwrap_schema, kwargs)

    #     op_schema = OpSchema(op_call._schema, args_schema, kwargs_schema)

    #     if _DEBUG_VERBOSE and torch.distributed.get_rank() == 0:
    #         print(f"OpSchema({op_schema})")
    #         local_shapes = tree_map(
    #             lambda t: t.to_local().shape
    #             if isinstance(t, dtensor.DTensor)
    #             else None,
    #             args,
    #         )
    #         print(f"    local shapes: {local_shapes}")

    #     return op_schema

    def run_op_prop(self, op_node: Node) -> None:
        """
        Propagate the sharding for an operator given the op_schema.
        """
        op_call = op_node.target
        # then we propagate the sharding
        sharding_prop_func = self.op_to_rules.get(op_call, None)

        if sharding_prop_func is None:
            # step 1. If there's not even one sharding rule
            # implemented for the operator, we error out.
            raise NotImplementedError(
                f"Operator {op_call} does not have a DistributedTensor rule registered."
            )

        for arg in op_node.args:
            print(f">>>>>. type of arg: {type(arg)}")
            # print(f">>>>>. meta of arg: {arg.meta}")

        args_schema = tree_map_only(Node, unwrap_spec_from_node, op_node.args)
        kwargs_schema = tree_map_only(Node, unwrap_spec_from_node, op_node.kwargs)
        print(">>>>>>???????success!!")

        op_schema = OpSchema(op_call._schema, args_schema, kwargs_schema)

        # step 2. there's sharding propagation rule, run
        # sharding propagation to get the output sharding
        try:
            output_sharding = sharding_prop_func(op_schema)
        except Exception as e:
            raise RuntimeError(
                f"Sharding propagation failed on op {op_call}.\n"
                f"Input schema: {op_schema}.\n"
                f"Error: {e}"
            ) from e

        # set the output sharding to the node
        op_node.meta["sharding"] = output_sharding

        # step 3. if can't get output_spec from sharding
        # propagation (i.e. no rules apply for input
        # placements), we return the output sharding
        # with schema suggestions, which can be used to
        # decide how to do redistribute on inputs
        if output_sharding.output_spec is None:
            if output_sharding.schema_suggestions is None:
                if output_sharding.failed_reason is not None:
                    raise RuntimeError(
                        f"Sharding propagation failed on op {op_call}!"
                        f"Input schema: {op_schema}."
                        f"Failed reason: {output_sharding.failed_reason}"
                    )
                else:
                    # if both output spec and schema suggestions are None, it
                    # means the operator return a non-tensor (scalar) value,
                    # in this case we just return the suggestion with the original
                    # input schema
                    output_sharding.schema_suggestions = [op_schema]
            else:
                # we do auto redistribute on inputs if necessary
                # to get an eligble input, which we will pick a
                # schema suggestion base on the redistribute cost.
                # For now we simply pick the first suggestion.
                # TODO: implement full auto distribute with a
                # simple cost estimation model
                suggested_input_schema = output_sharding.schema_suggestions[0]
                # run sharding propagation again with suggested schema
                propagation_res = sharding_prop_func(suggested_input_schema)
                # we set the output sharding with the new propagation result
                # so that dispatching know both output_spec and schema_suggestions
                # exist, which indicates a reshard is needed
                output_sharding.output_spec = propagation_res.output_spec
        else:
            # if sharding propagation succeed, we set the schema suggestion to
            # the default op_schema, which indicates no reshard is needed
            output_sharding.schema_suggestions = [op_schema]

        return output_sharding

    # def _propagate_tensor_meta(
    #     self,
    #     op_overload: OpOverload,
    #     op_schema: OpSchema,
    # ) -> Optional[torch.fx.Node]:
    #     # right now we only use the graph for metadata prop, but next we will use
    #     # the graph to do sharding prop together

    #     # special case op list, we don't need to propagate for local
    #     # scalar. TODO: figure out a better way to handle this
    #     skip_prop_list = [
    #         torch.ops.aten._local_scalar_dense.default,
    #         torch.ops.aten.equal.default,
    #     ]
    #     if op_overload in skip_prop_list:
    #         return None

    #     # NOTE: We must call the tracing in fake tensor mode so that it
    #     # avoids materializing memory
    #     with FakeTensorMode():
    #         fake_args = op_schema.gen_fake_args()
    #         fake_kwargs = op_schema.gen_fake_kwargs()
    #         g = get_isolated_graphmodule(op_overload, fake_args, fake_kwargs)

    #     output = None
    #     for node in g.graph.nodes:
    #         if node.op == "output":
    #             output = node
    #     return output


class _CachingPropagator(ShardingPropagator):
    """
    A sharding propagator that caches the propagation results.
    This is currently experimental for Tensor Parallel usage.
    """

    def __init__(self, op_to_rules=None) -> None:
        super().__init__()
        if op_to_rules is not None:
            self.op_to_rules = op_to_rules

        # cache table for sharding propagation results, we might need to
        # limit the size of the cache table in the future
        self.cached_prop_results: Dict[OpSchema, OutputSharding] = {}

    def propagate(
        self, op_call: OpOverload, op_schema: OpSchema
    ) -> OutputSharding:
        """
        Propagate the sharding for an operator given the op_schema.
        Cache the propagation results to avoid running propagation again.
        """
        if op_schema in self.cached_prop_results:
            return self.cached_prop_results[op_schema]
        else:
            # call DTensor's propagate_op_sharding to get the prop result
            output_sharding = super().propagate(op_call, op_schema)
            # update cached table
            self.cached_prop_results[op_schema] = output_sharding
            return output_sharding
