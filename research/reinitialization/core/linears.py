from matplotlib.pyplot import scatter
import torch
import torch.nn as nn
import torch.nn.functional as F

from lizrd.core import misc
from lizrd.support import ash
from research.reinitialization.core.pruner import Pruner
import plotly_express as px
from clearml import Logger
import numpy as np


def mask_by_score(
    mask: torch.Tensor, scores: torch.Tensor, n_to_mask: int
) -> torch.Tensor:
    """`n_to_mask` `mask` entries with the lowest `scores` will be pruned."""
    assert mask.shape == scores.shape

    mask = torch.clone(mask).detach()
    scores = torch.clone(scores).detach()

    # Determine indices of least important elements
    scores[mask == 0] = torch.inf

    topk = torch.topk(torch.abs(scores).view(-1), n_to_mask, largest=False)

    mask.view(-1)[topk.indices] = 0
    return mask


def create_mask(size: torch.Size) -> torch.nn.parameter.Parameter:
    mask = nn.parameter.Parameter(torch.ones(size), requires_grad=False)
    return mask


@ash.check("... inp -> ... out")
class PruneLinear(misc.Linear):
    """Linear layer with pruning"""

    def __init__(self, d_in, d_out, **kwargs):
        super().__init__(d_in, d_out, **kwargs)
        self.mask = create_mask(self.weight.shape)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        A = self.weight * self.mask
        res = misc.einsum("... i, o i -> ... o", x, A) + self.bias
        return res

    def prune(self, prob: float):
        self.mask.data = mask_by_score(
            self.mask, torch.rand_like(self.mask), round(self.mask.numel() * prob)
        )


@ash.check("... d -> ... d")
class UnstructPruneFF(nn.Module):
    def __init__(self, dmodel: int, dff: int, pruner: Pruner):
        super().__init__()
        self.lin1 = PruneLinear(dmodel, dff)
        self.lin2 = PruneLinear(dff, dmodel)
        pruner.register(self.lin1)
        pruner.register(self.lin2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.lin1(x)
        x = F.relu(x)
        x = self.lin2(x)
        return x


@ash.check("... d -> ... d")
class StructPruneFF(nn.Module):
    def __init__(self, dmodel: int, dff: int, pruner: Pruner):
        super().__init__()
        self.lin1 = nn.Linear(dmodel, dff)
        self.lin2 = nn.Linear(dff, dmodel)
        self.mask = create_mask(torch.Size([dff]))
        pruner.register(self)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.lin1(x)
        x = misc.einsum("... i, i -> ... i", x, self.mask)
        x = F.relu(x)
        x = self.lin2(x)
        return x

    def prune(self, prob: float):
        self.mask.data = mask_by_score(
            self.mask, torch.rand_like(self.mask), round(self.mask.numel() * prob)
        )


def prepare_for_logging(x):
    return x.view(-1).detach().cpu().numpy()


def prepare_subset_for_logging(xs, p=0.01):
    xs = [prepare_for_logging(x) for x in xs]
    random_indices = np.random.choice(len(xs[0]), int(len(xs[0]) * p), replace=False)
    return [x[random_indices] for x in xs]


@ash.check("... d -> ... d")
class LogFF(nn.Module):
    def __init__(self, dmodel: int, dff: int, pruner: Pruner):
        super().__init__()
        self.lin1 = misc.Linear(dmodel, dff)
        self.lin2 = misc.Linear(dff, dmodel)
        # torch.clone(self.lin1.weight).detach()
        # self.initial_weight1 = torch.clone(self.lin1.weight).detach()
        # self.initial_weight2 = torch.clone(self.lin2.weight).detach()
        self.reinforcement_count1 = nn.parameter.Parameter(
            torch.zeros(
                size=self.lin1.weight.shape, dtype=int, device=self.lin1.weight.device
            ),
            requires_grad=False,
        )
        self.reinforcement_count2 = nn.parameter.Parameter(
            torch.zeros(
                size=self.lin2.weight.shape, dtype=int, device=self.lin2.weight.device
            ),
            requires_grad=False,
        )

        self.initial_magnitudes = self.get_neurons_magnitudes()

        pruner.register(self)
        self.pruner = pruner

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.lin1(x)
        x = F.relu(x)
        x = self.lin2(x)
        return x

    def log_reinforcement_ratio(self, layer_name, step, ratio1, ratio2):
        logger = Logger.current_logger()
        logger.report_scalar(
            title=f"{layer_name} weight reinforcement ratio",
            series="lin1",
            value=ratio1,
            iteration=step,
        )
        logger.report_scalar(
            title=f"{layer_name} weight reinforcement ratio",
            series="lin2",
            value=ratio2,
            iteration=step,
        )

    def log_reinforcement_count_plot(self, layer_name, step):
        logger = Logger.current_logger()
        w1, rc1, w2, rc2 = prepare_subset_for_logging(
            [
                self.lin1.weight,
                self.reinforcement_count1,
                self.lin2.weight,
                self.reinforcement_count2,
            ]
        )
        fig1 = px.histogram(prepare_subset_for_logging(rc1))
        fig2 = px.histogram(prepare_subset_for_logging(rc2))
        logger.report_plotly(
            title=f"{layer_name} total reinforcement count",
            series="lin1",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} total reinforcement count",
            series="lin2",
            figure=fig2,
            iteration=step,
        )

    def after_backprop(self, layer_name, step):
        with torch.no_grad():
            diff1 = self.lin1.weight.grad * self.lin1.weight <= 0
            diff1_ratio = diff1.sum().item() / diff1.numel()
            self.reinforcement_count1 += diff1
            diff2 = self.lin2.weight.grad * self.lin2.weight <= 0
            diff2_ratio = diff2.sum().item() / diff2.numel()
            self.reinforcement_count2 += diff2
        self.log_reinforcement_ratio(layer_name, step, diff1_ratio, diff2_ratio)

    def get_neurons_grads_magnitudes(self):
        with torch.no_grad():
            grads1 = misc.einsum("i o -> i", self.lin1.weight.grad**2)
            grads2 = misc.einsum("o i -> i", self.lin2.weight.grad**2)
            magnitudes = grads1 * grads2
            return magnitudes

    def get_neurons_magnitudes(self):
        with torch.no_grad():
            weights1 = misc.einsum("i o -> i", self.lin1.weight**2)
            weights2 = misc.einsum("o i -> i", self.lin2.weight**2)
            magnitudes = weights1 * weights2
            return magnitudes

    def log_weights(self, layer_name, step, p=0.01):
        fig1 = px.histogram(prepare_subset_for_logging([self.lin1.weight], p)[0])
        fig2 = px.histogram(prepare_subset_for_logging([self.lin2.weight], p)[0])
        logger = Logger.current_logger()
        logger.report_plotly(
            title=f"{layer_name} weight",
            series="lin1",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} weight",
            series="lin2",
            figure=fig2,
            iteration=step,
        )

    def log_weights_grads(self, layer_name, step, p=0.01):
        fig1 = px.histogram(prepare_subset_for_logging([self.lin1.weight.grad], p)[0])
        fig2 = px.histogram(prepare_subset_for_logging([self.lin2.weight.grad], p)[0])
        logger = Logger.current_logger()
        logger.report_plotly(
            title=f"{layer_name} weight grad",
            series="lin1",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} weight grad",
            series="lin2",
            figure=fig2,
            iteration=step,
        )

    def log_neurons_magnitudes(self, layer_name, step) -> None:
        magnitudes = self.get_neurons_magnitudes()
        fig = px.histogram(prepare_for_logging(magnitudes))
        logger = Logger.current_logger()
        logger.report_plotly(
            title=f"{layer_name} neuron magnitude",
            series="magnitude",
            figure=fig,
            iteration=step,
        )

    def log_movement_weights(self, layer_name, step, p=0.01) -> None:
        movement1 = self.lin1.weight.cpu() - self.initial_weight1.cpu()
        movement2 = self.lin2.weight.cpu() - self.initial_weight2.cpu()
        mov1, mov2, w1, w2, in1, in2 = prepare_subset_for_logging(
            [
                movement1,
                movement2,
                self.lin1.weight,
                self.lin2.weight,
                self.initial_weight1,
                self.initial_weight2,
            ],
            p,
        )
        fig1 = px.scatter(x=in1, y=mov1)
        fig2 = px.scatter(x=in2, y=mov2)
        fig3 = px.scatter(x=in1, y=w1)
        fig4 = px.scatter(x=in2, y=w2)
        logger = Logger.current_logger()
        logger.report_plotly(
            title=f"{layer_name} weight movement",
            series="lin1 (x - initial weight, y - overall movement)",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} weight movement",
            series="lin2 (x - initial weight, y - overall movement)",
            figure=fig2,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} weight movement",
            series="lin1 (x - initial weight, y - current weight)",
            figure=fig3,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} weight movement",
            series="lin2 (x - initial weight, y - current weight)",
            figure=fig4,
            iteration=step,
        )

    def log_movement_weights_grads(self, layer_name, step, p=0.01) -> None:
        movement1 = self.lin1.weight.cpu() - self.initial_weight1.cpu()
        movement2 = self.lin2.weight.cpu() - self.initial_weight2.cpu()
        g1, g2, m1, m2 = prepare_subset_for_logging(
            [self.lin1.weight.grad, self.lin2.weight.grad, movement1, movement2], p
        )
        fig1 = px.scatter(x=g1, y=m1)
        fig2 = px.scatter(x=g2, y=m2)
        logger = Logger.current_logger()
        logger.report_plotly(
            title=f"{layer_name} weight grad movement",
            series="lin1 (x - current weight grad, y - overall movement)",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} weight grad movement",
            series="lin2 (x - current weight grad, y - overall movement)",
            figure=fig2,
            iteration=step,
        )

    def log_magnitude_movement_neurons(self, layer_name, step) -> None:
        logger = Logger.current_logger()
        mags = self.get_neurons_magnitudes()
        movement = mags.cpu() - self.initial_magnitudes.cpu()
        fig1 = px.scatter(
            x=prepare_for_logging(self.initial_magnitudes),
            y=prepare_for_logging(movement),
        )
        fig2 = px.scatter(x=prepare_for_logging(mags), y=prepare_for_logging(movement))
        fig3 = px.scatter(
            x=prepare_for_logging(mags), y=prepare_for_logging(self.initial_magnitudes)
        )
        logger.report_plotly(
            title=f"{layer_name} neuron magnitude movement",
            series="x - initial magnitudes, y - movement of magnitude",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} neuron magnitude movement",
            series="x - current magnitudes, y - movement of magnitude",
            figure=fig2,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} neuron magnitude movement",
            series="x - current magnitudes, y - initial magnitudes",
            figure=fig3,
            iteration=step,
        )

    def log_grad_magnitude_neurons(self, layer_name, step) -> None:
        logger = Logger.current_logger()
        mags = self.get_neurons_magnitudes()
        grads = self.get_neurons_grads_magnitudes()
        fig1 = px.scatter(
            x=prepare_for_logging(self.initial_magnitudes), y=prepare_for_logging(grads)
        )
        fig2 = px.scatter(x=prepare_for_logging(mags), y=prepare_for_logging(grads))
        fig3 = px.histogram(prepare_for_logging(grads))
        logger.report_plotly(
            title=f"{layer_name} neuron grad magnitude",
            series="x - initial magnitude, y - grad",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} neuron grad magnitude",
            series="x - current magnitude, y - grad",
            figure=fig2,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} neuron grad magnitude",
            series="current grad magnitudes",
            figure=fig3,
            iteration=step,
        )

    def log_grad_similar_to_dir(self, layer_name, step) -> None:
        """Measures how often the direction of the gradient is similar to the direction the neuron it pointing towards"""
        logger = Logger.current_logger()
        grad_good_1 = ((self.lin1.weight * self.lin1.weight.grad) < 0) * 1.0
        grad_good_2 = ((self.lin2.weight * self.lin2.weight.grad) < 0) * 1.0
        grad_good_neuron = misc.einsum("i o -> i", grad_good_1) + misc.einsum(
            "o i -> i", grad_good_2
        )
        mags = self.get_neurons_magnitudes()
        fig1 = px.histogram(prepare_subset_for_logging([grad_good_1], 0.01)[0])
        fig2 = px.histogram(prepare_subset_for_logging([grad_good_2], 0.01)[0])
        fig3 = px.scatter(
            x=prepare_for_logging(mags), y=prepare_for_logging(grad_good_neuron)
        )
        logger.report_plotly(
            title=f"{layer_name} direction",
            series="lin1 (sign of weight == -sign of grad)",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} direction",
            series="lin2 (sign of weight == -sign of grad)",
            figure=fig2,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} direction",
            series="neuron x - magnitude, y - (sign of weight == -sign of grad)",
            figure=fig3,
            iteration=step,
        )

    def log_grad_cosine_similarity(self, layer_name, step):
        # x - weight, y - cosine similarity
        # cosine similarity histogram
        logger = Logger.current_logger()

        neuron_weights = self.get_neurons_magnitudes()

        norms_weights1 = torch.sqrt(misc.einsum("i o -> i", self.lin1.weight**2))
        norms_grads1 = torch.sqrt(misc.einsum("i o -> i", self.lin1.weight.grad**2))
        similarity1 = misc.einsum(
            "i o -> i", self.lin1.weight * self.lin1.weight.grad
        ) / (norms_grads1 * norms_weights1)

        norms_weights2 = torch.sqrt(misc.einsum("o i -> i", self.lin2.weight**2))
        norms_grads2 = torch.sqrt(misc.einsum("o i -> i", self.lin2.weight.grad**2))
        similarity2 = misc.einsum(
            "o i -> i", self.lin2.weight * self.lin2.weight.grad
        ) / (norms_grads2 * norms_weights2)

        fig1 = px.histogram(prepare_for_logging(similarity1))
        fig2 = px.histogram(prepare_for_logging(similarity2))
        fig3 = px.scatter(
            x=prepare_for_logging(neuron_weights),
            y=prepare_for_logging(similarity1 + similarity2),
        )

        logger.report_plotly(
            title=f"{layer_name} cosine similarity",
            series="lin1",
            figure=fig1,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} cosine similarity",
            series="lin2",
            figure=fig2,
            iteration=step,
        )
        logger.report_plotly(
            title=f"{layer_name} cosine similarity",
            series="neuron x - magnitude, y - cosine similarity (sum between lin1 and lin2)",
            figure=fig3,
            iteration=step,
        )

    def prune(self, *args, **kwargs):
        pass

    def log(self, layer_name, step):
        logger = Logger.current_logger()
        with torch.no_grad():
            if False:
                self.log_neurons_magnitudes(layer_name, step)
                logger.flush(wait=True)

                self.log_weights(layer_name, step)
                logger.flush(wait=True)

                self.log_movement_weights(layer_name, step)
                logger.flush(wait=True)

                self.log_magnitude_movement_neurons(layer_name, step)
                logger.flush(wait=True)

                self.log_grad_similar_to_dir(layer_name, step)
                logger.flush(wait=True)

                self.log_grad_magnitude_neurons(layer_name, step)
                logger.flush(wait=True)

                self.log_weights_grads(layer_name, step)
                logger.flush(wait=True)

                self.log_movement_weights_grads(layer_name, step)
                logger.flush(wait=True)

            self.log_reinforcement_count_plot(layer_name, step)

            self.log_grad_cosine_similarity(layer_name, step)


class MagnitudePruneLinear(misc.Linear):
    """Linear layer with magnitude pruning"""

    def __init__(self, d_in, d_out, **kwargs):
        super().__init__(d_in, d_out, **kwargs)
        self.mask = create_mask(self.weight.shape)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        A = self.weight * self.mask
        res = misc.einsum("... i, o i -> ... o", x, A) + self.bias
        return res

    def prune(self, prob: float):
        self.mask.data = mask_by_score(
            self.mask, self.weight, round(self.mask.numel() * prob)
        )


@ash.check("... d -> ... d")
class UnstructMagnitudePruneFF(nn.Module):
    def __init__(self, dmodel: int, dff: int, pruner: Pruner):
        super().__init__()
        self.lin1 = MagnitudePruneLinear(dmodel, dff)
        self.lin2 = MagnitudePruneLinear(dff, dmodel)
        pruner.register(self.lin1)
        pruner.register(self.lin2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.lin1(x)
        x = F.relu(x)
        x = self.lin2(x)
        return x


@ash.check("... d -> ... d")
class StructMagnitudePruneFF(nn.Module):
    def __init__(self, dmodel: int, dff: int, pruner: Pruner):
        super().__init__()
        self.lin1 = nn.Linear(dmodel, dff)
        self.lin2 = nn.Linear(dff, dmodel)
        self.mask = create_mask(torch.Size([dff]))
        pruner.register(self)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.lin1(x)
        x = misc.einsum("... i, i -> ... i", x, self.mask)
        x = F.relu(x)
        x = self.lin2(x)
        return x

    def prune(self, prob: float):
        weights1 = misc.einsum("i o -> i", self.lin1.weight**2)
        weights2 = misc.einsum("o i -> i", self.lin2.weight**2)
        scores = weights1 * weights2
        self.mask.data = mask_by_score(
            self.mask, scores, round(self.mask.numel() * prob)
        )


@ash.check("... d -> ... d")
class MaskedFF(nn.Module):
    """Fully masked Feed-Forward layer"""

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.zeros_like(x)
