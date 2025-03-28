import torch
import numpy as np

import normflows as nf

from simtk import openmm as mm
from simtk import unit
from openmm import app
from openmmtools import testsystems
import mdtraj

from .flows import CoordinateTransform
from .distributions import Boltzmann, TransformedBoltzmann, \
    BoltzmannParallel, TransformedBoltzmannParallel

class BoltzmannGenerator(nf.NormalizingFlow):
    """
    Boltzmann Generator with architecture inspired by arXiv:2002.06707
    """
    def __init__(self, config):
        """
        Constructor
        :param config: Dict, specified by a yaml file, see sample config file
        """

        self.config = config
        # Set up simulation object
        if config['system']['name'] == 'AlanineDipeptideVacuum':
            ndim = 66
            z_matrix = [
                (0, [1, 4, 6]),
                (1, [4, 6, 8]),
                (2, [1, 4, 0]),
                (3, [1, 4, 0]),
                (4, [6, 8, 14]),
                (5, [4, 6, 8]),
                (7, [6, 8, 4]),
                (11, [10, 8, 6]),
                (12, [10, 8, 11]),
                (13, [10, 8, 11]),
                (15, [14, 8, 16]),
                (16, [14, 8, 6]),
                (17, [16, 14, 15]),
                (18, [16, 14, 8]),
                (19, [18, 16, 14]),
                (20, [18, 16, 19]),
                (21, [18, 16, 19])
            ]
            cart_indices = [6, 8, 9, 10, 14]
            temperature = config['system']['temperature']

            if config['system']['constraints']:
                self.system = testsystems.AlanineDipeptideVacuum()
            else:
                self.system = testsystems.AlanineDipeptideVacuum(constraints=None)
            if config['system']['platform'] == 'CPU':
                self.sim = app.Simulation(self.system.topology, self.system.system,
                                          mm.LangevinIntegrator(temperature * unit.kelvin,
                                                                1. / unit.picosecond,
                                                                1. * unit.femtosecond),
                                          mm.Platform.getPlatformByName('CPU'))
            elif config['system']['platform'] == 'Reference':
                self.sim = app.Simulation(self.system.topology, self.system.system,
                                          mm.LangevinIntegrator(temperature * unit.kelvin,
                                                                1. / unit.picosecond,
                                                                1. * unit.femtosecond),
                                          mm.Platform.getPlatformByName('Reference'))
            else:
                self.sim = app.Simulation(self.system.topology, self.system.system,
                                          mm.LangevinIntegrator(temperature * unit.kelvin,
                                                                1. / unit.picosecond,
                                                                1. * unit.femtosecond),
                                          mm.Platform.getPlatformByName(config['system']['platform']),
                                          {'Precision': config['system']['precision']})

            training_data = torch.randn(66, 66)
        else:
            raise NotImplementedError('The system ' + config['system']['name']
                                      + ' has not been implemented.')

        # Load data for transform if specified
        if config['data_path'] is not None:
            # Load the alanine dipeptide trajectory
            traj = mdtraj.load(config['data_path'])
            traj.center_coordinates()

            # superpose on the backbone
            ind = traj.top.select("backbone")

            traj.superpose(traj, 0, atom_indices=ind, ref_atom_indices=ind)

            # Gather the training data into a pytorch Tensor with the right shape
            training_data = traj.xyz
            n_atoms = training_data.shape[1]
            n_dim = n_atoms * 3
            training_data_npy = training_data.reshape(-1, n_dim)
            training_data = torch.from_numpy(training_data_npy.astype("float64"))

        # Set up model
        # Define flows
        rnvp_blocks = config['model']['rnvp']['blocks']

        # Set target distribution
        energy_cut = config['system']['energy_cut']
        energy_max = config['system']['energy_max']
        transform = CoordinateTransform(training_data, ndim, z_matrix, cart_indices)

        if 'parallel_energy' in config['system'] and config['system']['parallel_energy']:
            p = BoltzmannParallel(self.system, temperature, energy_cut=energy_cut,
                          energy_max=energy_max, n_threads=config['system']['n_threads'])
            if config['model']['snf']['mcmc']:
                p_ = TransformedBoltzmannParallel(self.system, temperature,
                                                  energy_cut=energy_cut,
                                                  energy_max=energy_max,
                                                  transform=transform,
                                                  n_threads=config['system']['n_threads'])
        else:
            p = Boltzmann(self.sim.context, temperature, energy_cut=energy_cut,
                          energy_max=energy_max)
            if config['model']['snf']['mcmc']:
                p_ = TransformedBoltzmann(self.sim.context, temperature, energy_cut=energy_cut,
                                          energy_max=energy_max, transform=transform)

        # Set up parameters for flow layers
        hidden_units = config['model']['rnvp']['hidden_units']
        hidden_layers = config['model']['rnvp']['hidden_layers']
        output_fn = config['model']['rnvp']['output_fn']
        output_scale = config['model']['rnvp']['output_scale']
        init_zeros = config['model']['rnvp']['init_zeros']

        # Set up base distribution
        latent_size = config['model']['latent_size']
        if 'base' in config['model'] and config['model']['base'] == 'resampled':
            a = nf.nets.MLP([latent_size] + hidden_layers * [hidden_units] + [1],
                            output_fn="sigmoid", leaky=0.01)
            q0 = nf.distributions.ResampledGaussian(latent_size, a, 100, 0.01, trainable=False)
        else:
            q0 = nf.distributions.DiagGaussian(latent_size, trainable=False)

        # Set up flow layers
        b = torch.Tensor([1 if i % 2 == 0 else 0 for i in range(latent_size)])
        flows = []
        for i in range(rnvp_blocks):
            if not 'include' in config['model']['rnvp'].keys() or \
                    config['model']['rnvp']['include']:
                # Two alternating Real NVP layers
                s = nf.nets.MLP([latent_size] + hidden_layers * [hidden_units] + [latent_size],
                                output_fn=output_fn, output_scale=output_scale, init_zeros=init_zeros)
                t = nf.nets.MLP([latent_size] + hidden_layers * [hidden_units] + [latent_size])
                flows += [nf.flows.MaskedAffineFlow(b, s, t)]
                s = nf.nets.MLP([latent_size] + hidden_layers * [hidden_units] + [latent_size],
                                output_fn=output_fn, output_scale=output_scale, init_zeros=init_zeros)
                t = nf.nets.MLP([latent_size] + hidden_layers * [hidden_units] + [latent_size])
                flows += [nf.flows.MaskedAffineFlow(1 - b, s, t)]

            # ActNorm
            if config['model']['actnorm']:
                flows += [nf.flows.ActNorm(latent_size)]

            # MCMC layer
            if config['model']['snf']['mcmc']:
                prop_scale = config['model']['snf']['proposal_std'] * np.ones(latent_size)
                proposal = nf.distributions.DiagGaussianProposal((latent_size,), prop_scale)
                steps = config['model']['snf']['steps']
                if 'lambda_min' in config['model']['snf'].keys() and \
                    'lambda_max' in config['model']['snf'].keys():
                    lam_min = config['model']['snf']['lambda_min']
                    lam_max = config['model']['snf']['lambda_max']
                    for j in range(steps):
                        lam = lam_min[i] + (lam_max[i] - lam_min[i]) * j / (steps - 1)
                        dist = nf.distributions.LinearInterpolation(p_, q0, lam)
                        flows += [nf.flows.MetropolisHastings(dist, proposal, 1)]
                else:
                    if 'lambda' in config['model']['snf'].keys():
                        lam = config['model']['snf']['lambda'][i]
                    else:
                        lam = (i + 1) / rnvp_blocks
                    dist = nf.distributions.LinearInterpolation(p_, q0, lam)
                    flows += [nf.flows.MetropolisHastings(dist, proposal, steps)]
        # Coordinate transformation
        flows += [transform]

        # Construct flow model
        super().__init__(q0=q0, flows=flows, p=p)

