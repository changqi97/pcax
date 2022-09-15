import pcaxrc.core as pcax
import pcaxrc.nn as nn
import jax
import jax.numpy as jnp
import optax
from pcaxrc.sli import DefaultState, Trainer
from pcaxrc.utils.optim import multi_transform

jax.config.update("jax_platform_name", "cpu")


class Model(pcax.Module):
    linear1: nn.Linear
    pc1: pcax.Layer

    def __init__(self, key) -> None:
        super().__init__()

        input_dim = 4
        hidden_dim = 128

        key, subkey = jax.random.split(key)
        self.linear1 = nn.Linear(input_dim, hidden_dim, _key=subkey)

        self.pc1 = pcax.Layer()

    def init(self, state, input_data):
        self.pc1.x.set(self.linear1(input_data))

        return state

    def __call__(self, x):
        x = self.pc1(self.linear1(x))
        x = self.pc1.view.children[1].get(self.pc1)

        return x


rseed = 0
rkey = jax.random.PRNGKey(rseed)
rkey, rsubkey = jax.random.split(rkey)

state = DefaultState()
state, model = state.init(
    Model(rsubkey),
    "*",
    batch_size=8,
    input_shape=(4,),
    # optim_fn=lambda state: multi_transform(
    #     {NODE_TYPE.X: optax.sgd(1e-4), NODE_TYPE.W: optax.sgd(1e-4)},
    #     state.get_masks("type"),
    # ),
)()

trainer = Trainer()

x = jnp.ones((4,))
model = trainer.init_fn(state, model, x)
trainer.update_fn(state, model, [x], loss_fn=lambda _, __, x: jnp.sum(x))

pass
