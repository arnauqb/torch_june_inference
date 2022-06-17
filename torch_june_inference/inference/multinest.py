from pathlib import Path
import pymultinest
from pymultinest.solve import solve
import yaml
import torch
import numpy as np
import pandas as pd
from scipy import stats

from torch_june import TorchJune
from torch_june_inference.utils import read_fortran_data_file
from torch_june_inference.inference.base import InferenceEngine
from torch_june_inference.paths import config_path


def read_pyro_to_scipy(dist, **kwargs):
    if dist == "Uniform":
        return stats.uniform(loc=kwargs["low"], scale=kwargs["high"] - kwargs["low"])
    elif dist == "Normal":
        return stats.norm(loc=kwargs["loc"], scale=kwargs["scale"])
    else:
        raise NotImplementedError


class MultiNest(InferenceEngine):
    @classmethod
    def read_parameters_to_fit(cls, params):
        parameters_to_fit = params["parameters_to_fit"]
        ret = {}
        for key in parameters_to_fit:
            ret[key] = read_pyro_to_scipy(**parameters_to_fit[key]["prior"])
        return ret

    def _prior(self, cube):
        """
        TODO: Need to invert from unit cube for other distros.
        """
        params = cube.copy()
        for i, key in enumerate(self.priors):
            params[i] = self.priors[key].ppf(cube[i])
        return params

    def _loglike(self, cube):
        # Set model parameters
        likelihood_fn = getattr(
            torch.distributions, self.inference_configuration["likelihood"]
        )
        with torch.no_grad():
            self.runner.reset_model()
            samples = {}
            for i, key in enumerate(self.priors):
                samples[key] = torch.tensor(cube[i], device=self.device)
            y, model_error = self.evaluate(samples)
            # Compare to data
            ret = 0.0
            for key in self.data_observable:
                time_stamps = self.data_observable[key]["time_stamps"]
                # data = y[key]
                data = y
                data_obs = self.observed_data[key][time_stamps]
                ret += (
                    likelihood_fn(data, model_error)
                    .log_prob(data_obs)
                    .sum()
                    .cpu()
                    .item()
                )
            return ret

    def run(self, **kwargs):
        ndims = len(self.priors)
        result = solve(
            LogLikelihood=self._loglike,
            Prior=self._prior,
            n_dims=ndims,
            outputfiles_basename=(self.results_path / "multinest").as_posix(),
            verbose=True,
            n_iter_before_update=1,
            resume=False,
            **kwargs,
        )
        self.results = self.save_results()

    def save_results(self):
        results = read_fortran_data_file(self.results_path / "multinest.txt")
        df = pd.DataFrame()
        df["likelihood"] = results[:, 1]
        for i, name in enumerate(self.priors):
            df[name] = results[:, 2 + i]
        df["weights"] = results[:, 0]
        df.to_csv(self.results_path / "results.csv")
        return df