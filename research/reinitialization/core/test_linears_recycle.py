import torch

from research.reinitialization import linears_recycle

from lizrd.support.test_utils import GeneralTestCase
from research.reinitialization.pruner import Pruner


class RecycleFFTest(GeneralTestCase):
    def _assert_perc_changed(self, weights_before, weights_after, perc):
        changed = torch.sum(weights_before != weights_after)
        total = torch.numel(weights_before)
        self.assertAlmostEqual((100 * changed / total).item(), perc, 1)


class TestUnstructMagnitudeRecycleFF(RecycleFFTest):
    def test_smoke(self):
        pruner = Pruner(1, 0.5)
        linears_recycle.UnstructMagnitudeRecycleFF(10, 2, pruner)
        linears_recycle.UnstructMagnitudeRecycleFF(10, 1, pruner)
        linears_recycle.UnstructMagnitudeRecycleFF(5, 5, pruner)

    def test_with_pruner(self):
        pruner = Pruner(2, 0.2)
        layer = linears_recycle.UnstructMagnitudeRecycleFF(100, 1000, pruner)
        weights_before_1 = layer.lin1.weight.data
        weights_before_2 = layer.lin2.weight.data

        for _ in range(2):
            pruner.step()

        # assert that number of changed is as expected
        self._assert_perc_changed(weights_before_1, layer.lin1.weight.data, 20)
        self._assert_perc_changed(weights_before_2, layer.lin2.weight.data, 20)

        weights_before_1 = layer.lin1.weight.data
        weights_before_2 = layer.lin2.weight.data

        for _ in range(2):
            pruner.step()

        # assert that number of changed is as expected
        self._assert_perc_changed(weights_before_1, layer.lin1.weight.data, 20)
        self._assert_perc_changed(weights_before_2, layer.lin2.weight.data, 20)

    def test_magnitude(self):
        pruner = Pruner(2, 0.001)
        layer = linears_recycle.UnstructMagnitudeRecycleFF(1000, 100, pruner)

        d = torch.diagonal(layer.lin1.weight.data)
        d *= 0
        r = layer.lin2.weight.data[2]
        r.fill_(0)

        for _ in range(2):
            pruner.step()

        d = torch.diagonal(layer.lin1.weight.data)
        assert torch.count_nonzero(d) == len(d)

        r = layer.lin2.weight.data[2]
        assert torch.count_nonzero(r) == len(d)


class TestStructMagnitudeRecycleFF(RecycleFFTest):
    def test_smoke(self):
        pruner = Pruner(1, 0.5)
        linears_recycle.StructMagnitudeRecycleFF(10, 2, pruner)
        linears_recycle.StructMagnitudeRecycleFF(10, 1, pruner)
        linears_recycle.StructMagnitudeRecycleFF(5, 5, pruner)

    def test_with_pruner(self):
        pruner = Pruner(2, 0.2)
        layer = linears_recycle.StructMagnitudeRecycleFF(100, 1000, pruner)
        weights_before_1 = layer.lin1.weight.data
        weights_before_2 = layer.lin2.weight.data

        for _ in range(2):
            pruner.step()

        # assert that number of changed is as expected
        self._assert_perc_changed(weights_before_1, layer.lin1.weight.data, 20)
        self._assert_perc_changed(weights_before_2, layer.lin2.weight.data, 20)

        weights_before_1 = layer.lin1.weight.data
        weights_before_2 = layer.lin2.weight.data

        for _ in range(2):
            pruner.step()

        # assert that number of changed is as expected
        self._assert_perc_changed(weights_before_1, layer.lin1.weight.data, 20)
        self._assert_perc_changed(weights_before_2, layer.lin2.weight.data, 20)

    def test_magnitude(self):
        pruner = Pruner(2, 0.1)
        layer = linears_recycle.StructMagnitudeRecycleFF(100, 10, pruner)

        layer.lin1.weight.data[7, :] *= 0
        layer.lin2.weight.data[:, 7] *= 0
        b_before = layer.lin1.bias.data
        b_before += 42

        for _ in range(2):
            pruner.step()

        row = layer.lin1.weight.data[7, :]
        assert torch.count_nonzero(row) == len(row)

        b_after = layer.lin1.bias.data
        assert b_before[7] != b_after[7]
        assert torch.count_nonzero(b_after != b_before) == 1

        col = layer.lin2.weight.data[:, 7]
        assert torch.count_nonzero(col) == len(col)
