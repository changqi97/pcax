from typing import Callable
import torch
import numpy as np
import torchvision
import torchvision.transforms as transforms
import os

# Core dependencies
import jax
import jax.numpy as jnp
import optax

# pcax
import pcax as px
import pcax.nn as pxnn
import pcax.utils as pxu
import pcax.functional as pxf
from omegacli import OmegaConf

# stune
import stune
import json


def se_loss(output, one_hot_label):
    return (jnp.square(output - one_hot_label)).sum()


# def ce_loss(output, one_hot_label):
#     return -(one_hot_label * jax.nn.log_softmax(output)).sum()


class AlexNet(px.Module):
    def __init__(self, nm_classes: int, act_fn: Callable[[jax.Array], jax.Array]) -> None:
        super().__init__()

        self.nm_classes = px.static(nm_classes)

        # Note we use a custom activation function and not exclusively ReLU since
        # it does not seem to perform as well as in backpropagation
        self.act_fn = px.static(act_fn)

        # We define the convolutional layers. We organise them in blocks just for clarity.
        # Ideally, pcax will soon support a "pxnn.Sequential" module to ease the definition
        # of such blocks. Layers are based on equinox.nn, so check their documentation for
        # more information.
        self.feature_layers = [
            (
                pxnn.Conv2d(3, 64, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1)),
                self.act_fn,
                pxnn.MaxPool2d(kernel_size=2, stride=2),
            ),
            (
                pxnn.Conv2d(64, 192, kernel_size=(3), padding=(1, 1)),
                self.act_fn,
                pxnn.MaxPool2d(kernel_size=2, stride=2),
            ),
            (pxnn.Conv2d(192, 384, kernel_size=(3, 3), padding=(1, 1)), self.act_fn),
            (pxnn.Conv2d(384, 256, kernel_size=(3, 3), padding=(1, 1)), self.act_fn),
            (
                pxnn.Conv2d(256, 256, kernel_size=(3, 3), padding=(1, 1)),
                self.act_fn,
                pxnn.MaxPool2d(kernel_size=2, stride=2),
            ),
        ]
        # We define the classifier layers. We organise them in blocks just for clarity.
        self.classifier_layers = [
            (pxnn.Linear(256 * 2 * 2, 4096), self.act_fn),
            (pxnn.Linear(4096, 4096), self.act_fn),
            (pxnn.Linear(4096, self.nm_classes.get()),),
        ]

        # self.long_skip_connection = pxnn.Linear(4096, self.nm_classes.get())

    def __call__(self, x: jax.Array):
        # Nothing new here: we just define the forward pass of the network by iterating
        # through the blocks and vodes. Each block is followed by a vode, to split the
        # computation in indpendent chunks.
        for i, block in enumerate(self.feature_layers):
            for layer in block:
                x = layer(x)

        x = x.flatten()
        for i, block in enumerate(self.classifier_layers):
            for layer in block:
                x = layer(x)

        return x


# This is a simple collate function that stacks numpy arrays used to interface
# the PyTorch dataloader with JAX. In the future we hope to provide custom dataloaders
# that are independent of PyTorch.


def numpy_collate(batch):
    if isinstance(batch[0], np.ndarray):
        return np.stack(batch)
    elif isinstance(batch[0], (tuple, list)):
        transposed = zip(*batch)
        return [numpy_collate(samples) for samples in transposed]
    else:
        return np.array(batch)


# The dataloader assumes cuda is being used, as such it sets 'pin_memory = True' and
# 'prefetch_factor = 2'. Note that the batch size should be constant during training, so
# we set 'drop_last = True' to avoid having to deal with variable batch sizes.
class TorchDataloader(torch.utils.data.DataLoader):
    def __init__(
        self,
        dataset,
        batch_size=1,
        shuffle=None,
        sampler=None,
        batch_sampler=None,
        num_workers=1,
        pin_memory=True,
        timeout=0,
        worker_init_fn=None,
        persistent_workers=True,
        prefetch_factor=2,
    ):
        super(self.__class__, self).__init__(
            dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            sampler=sampler,
            batch_sampler=batch_sampler,
            num_workers=num_workers,
            collate_fn=numpy_collate,
            pin_memory=pin_memory,
            drop_last=True if batch_sampler is None else None,
            timeout=timeout,
            worker_init_fn=worker_init_fn,
            persistent_workers=persistent_workers,
            prefetch_factor=prefetch_factor,
        )


def get_dataloaders(batch_size: int):
    t = transforms.Compose([
        transforms.RandomHorizontalFlip(p=0.5),
        # transforms.RandomRotation(5),
        transforms.RandomCrop(32, padding=4),
        transforms.ToTensor(),
        # These are normalisation factors found online.
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        lambda x: x.numpy()
    ])

    t_val = transforms.Compose([
        transforms.ToTensor(),
        # These are normalisation factors found online.
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        lambda x: x.numpy()
    ])

    train_dataset = torchvision.datasets.CIFAR10(
        "~/tmp/cifar10/",
        transform=t,
        download=False,
        train=True,
    )

    train_dataloader = TorchDataloader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=7,
    )

    test_dataset = torchvision.datasets.CIFAR10(
        "~/tmp/cifar10/",
        transform=t_val,
        download=False,
        train=False,
    )

    test_dataloader = TorchDataloader(
        test_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=7,
    )

    return train_dataloader, test_dataloader


@pxf.vmap({"model": None}, in_axes=0, out_axes=0)
def forward(x, *, model: AlexNet):
    return model(x)


@pxf.vmap({"model": None}, in_axes=(0, 0), out_axes=(None, 0), axis_name="batch")
def loss(x, y, *, model: AlexNet):
    y_ = model(x)
    return jax.lax.pmean(se_loss(y_, y), "batch"), y_


@pxf.jit()
def train_on_batch(x: jax.Array, y: jax.Array, *, model: AlexNet, optim_w: pxu.Optim):
    model.train()
    # Learning step
    with pxu.step(model):
        _, g = pxf.value_and_grad(pxu.Mask(pxnn.LayerParam, [False, True]), has_aux=True)(loss)(x, y,  model=model)
    optim_w.step(model, g["model"])
    

@pxf.jit()
def eval_on_batch(x: jax.Array, y: jax.Array, *, model: AlexNet):
    model.eval()

    with pxu.step(model):
        y_ = forward(x, model=model).argmax(axis=-1)

    return (y_ == y).mean(), y_


def train(dl, *, model: AlexNet, optim_w: pxu.Optim):
    for i, (x, y) in enumerate(dl):
        train_on_batch(
            x, jax.nn.one_hot(y, model.nm_classes.get()), model=model, optim_w=optim_w
        )


def eval(dl, *, model: AlexNet):
    acc = []
    ys_ = []

    for x, y in dl:
        a, y_ = eval_on_batch(x, y, model=model)
        acc.append(a)
        ys_.append(y_)

    return np.mean(acc), np.concatenate(ys_)


def main(run_info: stune.RunInfo):
    intermidiate_savepath = run_info["study"] + '.json' if run_info.study_name else None

    batch_size = run_info["hp/batch_size"]
    nm_epochs = run_info["hp/epochs"]

    model = AlexNet(
        nm_classes=10, 
        act_fn=getattr(jax.nn, run_info["hp/act_fn"])
    )
    
    train_dataloader, test_dataloader = get_dataloaders(batch_size)

    schedule = optax.warmup_cosine_decay_schedule(
        init_value=run_info["hp/optim/w/lr"],
        peak_value=1.1 * run_info["hp/optim/w/lr"],
        warmup_steps=0.1 * len(train_dataloader) * nm_epochs,
        decay_steps=len(train_dataloader)*nm_epochs,
        end_value=0.1 * run_info["hp/optim/w/lr"],
        exponent=1.0)

    # optim_h = pxu.Optim(
    #     optax.chain(
    #         optax.sgd(run_info["hp/optim/x/lr"], momentum=run_info["hp/optim/x/momentum"]),
    #     )
    # )
    optim_w = pxu.Optim(optax.adamw(schedule, weight_decay=run_info["hp/optim/w/wd"]), pxu.Mask(pxnn.LayerParam)(model))
    
    best_accuracy = 0
    acc_list = []
    below_times = 0
    stop_increase = 0
    
    for e in range(nm_epochs):
        # beta = run_info["hp/beta_factor"] * (run_info["hp/beta"] + run_info["hp/beta_ir"]*e)
        # if abs(beta) >= 1.0 :
        #     beta = 1.0
        train(train_dataloader, model=model, optim_w=optim_w)#, optim_h=optim_h, beta=beta)
        a, y = eval(test_dataloader, model=model)
        acc_list.append(float(a))
        if run_info.study_name is None:
            print(f"Epoch {e + 1}/{nm_epochs} - Test Accuracy: {a * 100:.2f}%")
        if a > best_accuracy:
            best_accuracy = a
            stop_increase = 0
        else:
            stop_increase += 1
        if float(a) < 0.15 or (e > 10 and float(a) < 0.5):
            below_times += 1
        else:
            below_times = 0
        if below_times >= 5 or (e >= 25 and stop_increase >= 5):
            break
    
    config_save = run_info.log
    config_save['results'] = acc_list
    if intermidiate_savepath:
        try:
            with open(intermidiate_savepath, 'r') as file:
                # load json file if exists
                data = json.load(file)
        except FileNotFoundError:
            data = []
        except json.decoder.JSONDecodeError:
            data = []

        # add new data to the list
        data.append(config_save)

        with open(intermidiate_savepath, 'w') as file:
            json.dump(data, file, indent=4)

    return best_accuracy


if __name__ == "__main__":
    import clock
    train = clock.timed_fn(train)
    run_info = stune.RunInfo(
        OmegaConf.load("BP_SE.yaml")
    )
    main(run_info)
