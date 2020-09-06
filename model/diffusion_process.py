import numpy as np

import torch

from model.base import BaseModule
from model.nn import WaveGradNN


class WaveGrad(BaseModule):
    """
    WaveGrad diffusion process as described in WaveGrad paper
    (link: https://arxiv.org/pdf/2009.00713.pdf).
    Implementation adopted from `Denoising Diffusion Probabilistic Models`
    repository (link: https://github.com/hojonathanho/diffusion,
    paper: https://arxiv.org/pdf/2006.11239.pdf).

    Note:
        * Prefer using `sample_subregions_parallel` method to generate samples.
          More details in `sample_subregions_parallel` method docs.
    """
    def __init__(self, config):
        super(WaveGrad, self).__init__()
        self.n_iter = config.model_config.noise_schedule.n_iter
        self.betas_range = config.model_config.noise_schedule.betas_range
        self.mel_segment_length = config.training_config.segment_length//config.data_config.hop_length

        betas = torch.linspace(self.betas_range[0], self.betas_range[1], steps=self.n_iter)
        alphas = 1 - betas
        alphas_cumprod = alphas.cumprod(dim=0)
        alphas_cumprod_prev = torch.cat([torch.FloatTensor([1]), alphas_cumprod[:-1]])
        alphas_cumprod_prev_with_last = torch.cat([torch.FloatTensor([1]), alphas_cumprod])
        self.register_buffer('betas', betas)
        self.register_buffer('alphas', alphas)
        self.register_buffer('alphas_cumprod', alphas_cumprod)
        self.register_buffer('alphas_cumprod_prev', alphas_cumprod_prev)

        # Calculations for posterior q(y_n|y_0)
        sqrt_alphas_cumprod = alphas_cumprod.sqrt()
        # For WaveGrad special continiout noise level conditioning
        self.sqrt_alphas_cumprod_prev = alphas_cumprod_prev_with_last.sqrt().numpy()
        sqrt_recip_alphas_cumprod = (1 / alphas_cumprod).sqrt()
        sqrt_recipm1_alphas_cumprod = (1 / alphas_cumprod - 1).sqrt()
        self.register_buffer('sqrt_alphas_cumprod', sqrt_alphas_cumprod)
        self.register_buffer('sqrt_recip_alphas_cumprod', sqrt_recip_alphas_cumprod)
        self.register_buffer('sqrt_recipm1_alphas_cumprod', sqrt_recipm1_alphas_cumprod)

        # Calculations for posterior q(y_{t-1} | y_t, y_0)
        posterior_variance = betas * (1 - alphas_cumprod_prev) / (1 - alphas_cumprod)
        posterior_variance = torch.stack([posterior_variance, torch.FloatTensor([1e-20] * self.n_iter)])
        posterior_log_variance_clipped = posterior_variance.max(dim=0).values.log()
        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
        posterior_mean_coef1 = betas * alphas_cumprod_prev.sqrt() / (1 - alphas_cumprod)
        posterior_mean_coef2 = (1 - alphas_cumprod_prev) * alphas.sqrt() / (1 - alphas_cumprod)
        self.register_buffer('posterior_log_variance_clipped', posterior_log_variance_clipped)
        self.register_buffer('posterior_mean_coef1', posterior_mean_coef1)
        self.register_buffer('posterior_mean_coef2', posterior_mean_coef2)

        # Backbone neural network to model noise
        self.total_factor = np.product(config.model_config.factors)
        assert self.total_factor == config.data_config.hop_length, \
            """Total factor-product should be equal to the hop length of STFT. Other cases have not been tested yet."""
        self.n_iter = config.model_config.noise_schedule.n_iter
        self.nn = WaveGradNN(config)

    def sample_continious_noise_level(self, batch_size, device):
        """
        Samples continious noise level sqrt(alpha_cumprod).
        This is what makes WaveGrad different from other Denoising Diffusion Probabilistic Models.
        """
        s = np.random.choice(range(1, self.n_iter + 1), size=batch_size)
        continious_sqrt_alpha_cumprod = torch.FloatTensor(
            np.random.uniform(
                self.sqrt_alphas_cumprod_prev[s-1],
                self.sqrt_alphas_cumprod_prev[s],
                size=batch_size
            )
        ).to(device)
        return continious_sqrt_alpha_cumprod.unsqueeze(-1)
    
    def q_sample(self, y_0, continious_sqrt_alpha_cumprod=None, eps=None):
        batch_size = y_0.shape[0]
        continious_sqrt_alpha_cumprod \
            = self.sample_continious_noise_level(batch_size, device=y_0.device) \
                if isinstance(eps, type(None)) else continious_sqrt_alpha_cumprod
        if isinstance(eps, type(None)):
            eps = torch.randn_like(y_0)
        outputs = continious_sqrt_alpha_cumprod * y_0 + (1 - continious_sqrt_alpha_cumprod**2) * eps
        return outputs

    def q_posterior(self, y_start, y, t):
        posterior_mean = self.posterior_mean_coef1[t] * y_start + self.posterior_mean_coef2[t] * y
        posterior_log_variance_clipped = self.posterior_log_variance_clipped[t]
        return posterior_mean, posterior_log_variance_clipped

    def predict_start_from_noise(self, y, t, eps):
        return self.sqrt_recip_alphas_cumprod[t] * y - self.sqrt_recipm1_alphas_cumprod[t] * eps

    def p_mean_variance(self, mels, y, t, clip_denoised: bool):
        batch_size = mels.shape[0]
        noise_level = torch.FloatTensor([self.sqrt_alphas_cumprod_prev[t]]).repeat(batch_size, 1).to(mels)
        eps_recon = self.nn(mels, y, noise_level)
        y_recon = self.predict_start_from_noise(y, t, eps_recon)

        if clip_denoised:
            y_recon.clamp_(-1.0, 1.0)
        
        model_mean, posterior_log_variance = self.q_posterior(y_start=y_recon, y=y, t=t)
        return model_mean, posterior_log_variance

    def compute_inverse_dynamics(self, mels, y, t, clip_denoised=True):
        """
        Computes Langevin inverse dynamics.
        :param mels (torch.Tensor): mel-spectrograms acoustic features of shape [B, n_mels, T//hop_length]
        :param y (torch.Tensor): previous state from dynamics trajectory
        :param clip_denoised (bool, optional): clip signal to [-1, 1]
        :return (torch.Tensor): next state
        """
        model_mean, model_log_variance = self.p_mean_variance(mels, y, t, clip_denoised)
        eps = torch.randn_like(y) if t > 0 else torch.zeros_like(y)
        return model_mean + eps * (0.5 * model_log_variance).exp()

    def sample(self, mels, store_intermediate_states=False):
        """
        Generation from mel-spectrograms.
        :param mels (torch.Tensor): mel-spectrograms acoustic features of shape [B, n_mels, T//hop_length]
        :param store_intermediate_states (bool, optional): whether to store dynamics trajectory or not
        :return ys (list of torch.Tensor) (if store_intermediate_states=True)
            or y_0 (torch.Tensor): predicted signals on every dynamics iteration of shape [B, T]
        """
        with torch.no_grad():
            device = next(self.parameters()).device
            batch_size, T = mels.shape[0], mels.shape[-1]
            ys = [torch.randn(batch_size, T*self.total_factor, dtype=torch.float32).to(device)]
            t = self.n_iter - 1
            while t >= 0:
                y_t = self.compute_inverse_dynamics(mels, y=ys[-1], t=t)
                ys.append(y_t)
                t -= 1
            return ys if store_intermediate_states else ys[-1]

    def sample_subregions_parallel(self, mels, store_intermediate_states=False):
        """
        Generation from mel-spectrogram by splitting inputs into several parts and processing them in paralell.
        Motivation is about the fact, that during training the model has seen only small segments of
        speech, thus it will fail on longer sequences because of positional encoding (rememeber sin-cos visualization of PE).
        Experiments have showed significant improvement in waveform generation using parallel generation.
        :param mels (torch.Tensor): mel-spectrograms acoustic features of shape [B, n_mels, T//hop_length]
        :param store_intermediate_states (bool, optional): whether to store dynamics trajectory or not
        :return ys (list of torch.Tensor) (if store_intermediate_states=True)
            or y_0 (torch.Tensor): predicted signals on every dynamics iteration of shape [B, T]
        """
        # @TODO: make hops between splits to reduce clicks on endges.
        with torch.no_grad():
            splits = mels.split(self.mel_segment_length, dim=-1)
            recons = []
            for split in splits:
                outputs = self.sample(mels=split, store_intermediate_states=store_intermediate_states)
                if store_intermediate_states:
                    outputs = torch.stack(outputs)
                recons.append(outputs)
            final_outputs = torch.cat(recons, dim=-1)
            return final_outputs

    def compute_loss(self, mels, y_0):
        """
        Computes loss between GT Gaussian noise and predicted noise by model from diffusion process.
        :param mels (torch.Tensor): mel-spectrograms acoustic features of shape [B, n_mels, T//hop_length]
        :param y_0 (torch.Tensor): GT speech signals
        :return loss (torch.Tensor): loss of diffusion model
        """
        # Sample continious noise level
        batch_size = y_0.shape[0]
        continious_sqrt_alpha_cumprod \
            = self.sample_continious_noise_level(batch_size, device=y_0.device)
        eps = torch.randn_like(y_0)

        # Diffuse the signal
        y_noisy = self.q_sample(y_0, continious_sqrt_alpha_cumprod, eps)

        # Reconstruct the added noise
        eps_recon = self.nn(mels, y_noisy, continious_sqrt_alpha_cumprod)
        loss = torch.nn.L1Loss()(eps_recon, eps)
        return loss