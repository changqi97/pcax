__all__ = [
    "cond",
    "switch",
    "scan",
]


import jax
from typing import Union, Callable, Tuple, Optional, Any

from ..core.transform import _AbstractTransformation
from ..core.modules import ParamDict
from ..core.filter import f


class cond(_AbstractTransformation):
    def __init__(
        self,
        true_fn: Union[_AbstractTransformation, Callable],
        false_fn: Union[_AbstractTransformation, Callable],
        filter: Union[f, Callable[[ParamDict], ParamDict]] = lambda *args: True,
    ):
        super().__init__((true_fn, false_fn), filter)

    def _call(self, cached_kwargs, *args):
        target_params, other_params = cached_kwargs['params']
        output, (target_params, other_params) = self.transform(
            (target_params, other_params),
            *args
        )

        return output

    def _make_transform(self, fns, cached_kwargs):
        return lambda params_partition, cond, *args: jax.lax.cond(
            cond,
            *tuple(self._functional(fn, cached_kwargs) for fn in fns),
            params_partition,
            *args,
        )


class switch(_AbstractTransformation):
    def __init__(
        self,
        fns: Tuple[Union[_AbstractTransformation, Callable], ...],
        filter: Union[f, Callable[[ParamDict], ParamDict]] = lambda *args: True,
    ):
        super().__init__(fns, filter)

    def _call(self, cached_kwargs, *args):
        target_params, other_params = cached_kwargs['params']
        output, (target_params, other_params) = self.transform(
            (target_params, other_params),
            *args
        )

        return output

    def _make_transform(self, fns, cached_kwargs):
        return lambda params_partition, j, *args: jax.lax.switch(
            j,
            tuple(self._functional(fn, cached_kwargs) for fn in fns),
            params_partition,
            *args,
        )


class scan(_AbstractTransformation):
    def __init__(
        self,
        fn: Union[_AbstractTransformation, Callable],
        js: Optional[Union[jax.Array, Any]] = None,
        length: Optional[int] = None,
        map_outputs: Tuple[int, ...] = (),
        filter: Union[f, Callable[[ParamDict], ParamDict]] = lambda *args: True,
    ):
        assert sum((js is not None, length is not None)) == 1, \
            "Exactly one between 'js' and 'length' must be specified"

        super().__init__(fn, filter)

        self.js = js
        self.length = length
        self.map_outputs = map_outputs

    def _call(self, cached_kwargs, *args):
        if self.js is not None:
            args = (None,) + args

        target_params, other_params = cached_kwargs['params']
        ((target_params, other_params), args), output = self.transform(
            (target_params, other_params),
            *args
        )

        return output, *args

    def _make_transform(self, fn, cached_kwargs):
        def scan(
            carry, j
        ):
            params_partition, args_list = carry

            if self.js is not None:
                r, params_partition = self._functional(fn, cached_kwargs)(params_partition, j, *args_list[1:])
            else:
                r, params_partition = self._functional(fn, cached_kwargs)(params_partition, *args_list)

            # Update args
            if isinstance(r, tuple):
                if len(r) == 2 and isinstance(r[0], tuple):
                    updated_args = r[0]
                    y = r[1]
                else:
                    updated_args = r
                    y = None

                updated_args = r[0]
                for updated_arg, map_output in zip(
                    updated_args,
                    self.map_outputs + tuple(range(len(updated_args) - len(self.map_outputs)))
                ):
                    args_list[map_output] = updated_arg
            else:
                y = r

            return (params_partition, args_list), y

        return lambda params_partition, *args: jax.lax.scan(scan, (params_partition, args), self.js, self.length)


class while_loop(_AbstractTransformation):

    def __init__(
        self,
        fn: Union[_AbstractTransformation, Callable],
        cond_fn: Callable,
        map_outputs: Tuple[int, ...] = (),
        filter: Union[f, Callable[[ParamDict], ParamDict]] = lambda *args: True,
    ):
        """while_loop constructor.

            Args:
            fn: function corresponding to `body_fun` for jax.lax.while_loop,
            cond_fn: function corresponding to `cond_fun` for jax.lax.while_loop,
            filter: selects which params to apply the transformation to [
                it is used by vmap, grad, ... to select which params to be targeted by those transformations.
                There is no apparent use of it for flow transformations, but maybe I'm missing it;
                so there is still an option to specify it
            ],
        """
        super().__init__(fn, filter)

        self.cond_fn = cond_fn
        self.map_outputs = map_outputs

    def _call(self, cached_kwargs, *args):
        target_params, other_params = cached_kwargs['params']
        (target_params, other_params), output = self.transform(
            (target_params, other_params),
            *args
        )

        return output

    def _make_transform(self, fn, cached_kwargs):
        def while_loop(
            carry
        ):
            params_partition, args_list = carry
            updated_args, params_partition = self._functional(fn, cached_kwargs)(params_partition, *args_list)

            return (params_partition, updated_args)

        return lambda params_partition, *args: jax.lax.while_loop(
            lambda carry: self.cond_fn(*carry[1]),
            while_loop,
            (params_partition, args)
        )
