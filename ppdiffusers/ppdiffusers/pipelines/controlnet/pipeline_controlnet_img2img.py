import paddle
import paddlenlp
import inspect
import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import numpy as np
import PIL.Image
from ...image_processor import VaeImageProcessor
from ...loaders import FromSingleFileMixin, LoraLoaderMixin, TextualInversionLoaderMixin
from ...models import AutoencoderKL, ControlNetModel, UNet2DConditionModel
from ...schedulers import KarrasDiffusionSchedulers
from ...utils import deprecate, is_compiled_module, logging, randn_tensor, replace_example_docstring
from ..pipeline_utils import DiffusionPipeline
from ..stable_diffusion import StableDiffusionPipelineOutput
from ..stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from .multicontrolnet import MultiControlNetModel
logger = logging.get_logger(__name__)
EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> # !pip install opencv-python 
        >>> from ppdiffusers import StableDiffusionControlNetImg2ImgPipeline, ControlNetModel, UniPCMultistepScheduler
        >>> from ppdiffusers.utils import load_image
        >>> import numpy as np
        >>> import paddle

        >>> import cv2
        >>> from PIL import Image

        >>> # download an image
        >>> image = load_image(
        ...     "https://hf.co/datasets/huggingface/documentation-images/resolve/main/diffusers/input_image_vermeer.png"
        ... )
        >>> np_image = np.array(image)

        >>> # get canny image
        >>> np_image = cv2.Canny(np_image, 100, 200)
        >>> np_image = np_image[:, :, None]
        >>> np_image = np.concatenate([np_image, np_image, np_image], axis=2)
        >>> canny_image = Image.fromarray(np_image)

        >>> # load control net and stable diffusion v1-5
        >>> controlnet = ControlNetModel.from_pretrained("lllyasviel/sd-controlnet-canny", paddle_dtype=paddle.float16)
        >>> pipe = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
        ...     "runwayml/stable-diffusion-v1-5", controlnet=controlnet, paddle_dtype=paddle.float16
        ... )

        >>> # speed up diffusion process with faster scheduler and memory optimization
        >>> pipe.scheduler = UniPCMultistepScheduler.from_config(pipe.scheduler.config)

        >>> # generate image
        >>> generator = paddle.Generator().manual_seed(0)
        >>> image = pipe(
        ...     "futuristic-looking woman",
        ...     num_inference_steps=20,
        ...     generator=generator,
        ...     image=image,
        ...     control_image=canny_image,
        ... ).images[0]
        ```
"""


def prepare_image(image):
    if isinstance(image, paddle.Tensor):
        if image.ndim == 3:
            image = image.unsqueeze(axis=0)
        image = image.cast(dtype='float32')
    else:
        if isinstance(image, (PIL.Image.Image, np.ndarray)):
            image = [image]
        if isinstance(image, list) and isinstance(image[0], PIL.Image.Image):
            image = [np.array(i.convert('RGB'))[(None), :] for i in image]
            image = np.concatenate(image, axis=0)
        elif isinstance(image, list) and isinstance(image[0], np.ndarray):
            image = np.concatenate([i[(None), :] for i in image], axis=0)
        image = image.transpose(0, 3, 1, 2)
        image = paddle.to_tensor(data=image).cast(dtype='float32') / 127.5 - 1.0
    return image


class StableDiffusionControlNetImg2ImgPipeline(
        DiffusionPipeline, TextualInversionLoaderMixin, LoraLoaderMixin,
        FromSingleFileMixin):
    """
    Pipeline for text-to-image generation using Stable Diffusion with ControlNet guidance.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    In addition the pipeline inherits the following loading methods:
        - *Textual-Inversion*: [`loaders.TextualInversionLoaderMixin.load_textual_inversion`]

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations.
        text_encoder ([`CLIPTextModel`]):
            Frozen text-encoder. Stable Diffusion uses the text portion of
            [CLIP](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel), specifically
            the [clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14) variant.
        tokenizer (`CLIPTokenizer`):
            Tokenizer of class
            [CLIPTokenizer](https://huggingface.co/docs/transformers/v4.21.0/en/model_doc/clip#transformers.CLIPTokenizer).
        unet ([`UNet2DConditionModel`]): Conditional U-Net architecture to denoise the encoded image latents.
        controlnet ([`ControlNetModel`] or `List[ControlNetModel]`):
            Provides additional conditioning to the unet during the denoising process. If you set multiple ControlNets
            as a list, the outputs from each ControlNet are added together to create one combined additional
            conditioning.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
        safety_checker ([`StableDiffusionSafetyChecker`]):
            Classification module that estimates whether generated images could be considered offensive or harmful.
            Please, refer to the [model card](https://huggingface.co/runwayml/stable-diffusion-v1-5) for details.
        feature_extractor ([`CLIPImageProcessor`]):
            Model that extracts features from generated images to be used as inputs for the `safety_checker`.
    """
    _optional_components = ['safety_checker', 'feature_extractor']

    def __init__(
            self,
            vae: AutoencoderKL,
            text_encoder: paddlenlp.transformers.CLIPTextModel,
            tokenizer: paddlenlp.transformers.CLIPTokenizer,
            unet: UNet2DConditionModel,
            controlnet: Union[ControlNetModel, List[ControlNetModel], Tuple[
                ControlNetModel], MultiControlNetModel],
            scheduler: KarrasDiffusionSchedulers,
            safety_checker: StableDiffusionSafetyChecker,
            feature_extractor: paddlenlp.transformers.CLIPImageProcessor,
            requires_safety_checker: bool=True):
        super().__init__()
        if safety_checker is None and requires_safety_checker:
            logger.warning(
                f'You have disabled the safety checker for {self.__class__} by passing `safety_checker=None`. Ensure that you abide to the conditions of the Stable Diffusion license and do not expose unfiltered results in services or applications open to the public. Both the diffusers team and Hugging Face strongly recommend to keep the safety filter enabled in all public facing circumstances, disabling it only for use-cases that involve analyzing network behavior or auditing its results. For more information, please have a look at https://github.com/huggingface/diffusers/pull/254 .'
            )
        if safety_checker is not None and feature_extractor is None:
            raise ValueError(
                "Make sure to define a feature extractor when loading {self.__class__} if you want to use the safety checker. If you do not want to use the safety checker, you can pass `'safety_checker=None'` instead."
            )
        if isinstance(controlnet, (list, tuple)):
            controlnet = MultiControlNetModel(controlnet)
        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            controlnet=controlnet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor)
        self.vae_scale_factor = 2**(len(self.vae.config.block_out_channels) - 1)
        self.image_processor = VaeImageProcessor(
            vae_scale_factor=self.vae_scale_factor, do_convert_rgb=True)
        self.control_image_processor = VaeImageProcessor(
            vae_scale_factor=self.vae_scale_factor,
            do_convert_rgb=True,
            do_normalize=False)
        self.register_to_config(requires_safety_checker=requires_safety_checker)

    def _encode_prompt(self,
                       prompt,
                       num_images_per_prompt,
                       do_classifier_free_guidance,
                       negative_prompt=None,
                       prompt_embeds: Optional[paddle.Tensor]=None,
                       negative_prompt_embeds: Optional[paddle.Tensor]=None,
                       lora_scale: Optional[float]=None):
        """
        Encodes the prompt into text encoder hidden states.

        Args:
             prompt (`str` or `List[str]`, *optional*):
                prompt to be encoded
            num_images_per_prompt (`int`):
                number of images that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. If not defined, one has to pass
                `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
                less than `1`).
            prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
            lora_scale (`float`, *optional*):
                A lora scale that will be applied to all LoRA layers of the text encoder if LoRA layers are loaded.
        """
        if lora_scale is not None and isinstance(self, LoraLoaderMixin):
            self._lora_scale = lora_scale
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]
        if prompt_embeds is None:
            if isinstance(self, TextualInversionLoaderMixin):
                prompt = self.maybe_convert_prompt(prompt, self.tokenizer)
            text_inputs = self.tokenizer(
                prompt,
                padding='max_length',
                max_length=self.tokenizer.model_max_length,
                truncation=True,
                return_tensors='pd')
            text_input_ids = text_inputs.input_ids
            untruncated_ids = self.tokenizer(
                prompt, padding='longest', return_tensors='pd').input_ids
            if untruncated_ids.shape[-1] >= text_input_ids.shape[
                    -1] and not paddle.equal_all(
                        x=text_input_ids, y=untruncated_ids).item():
                removed_text = self.tokenizer.batch_decode(
                    untruncated_ids[:, self.tokenizer.model_max_length - 1:-1])
                logger.warning(
                    f'The following part of your input was truncated because CLIP can only handle sequences up to {self.tokenizer.model_max_length} tokens: {removed_text}'
                )
            if hasattr(self.text_encoder.config, 'use_attention_mask'
                       ) and self.text_encoder.config.use_attention_mask:
                attention_mask = text_inputs.attention_mask
            else:
                attention_mask = None
            prompt_embeds = self.text_encoder(
                text_input_ids, attention_mask=attention_mask)
            prompt_embeds = prompt_embeds[0]
        prompt_embeds = prompt_embeds.cast(dtype=self.text_encoder.dtype)
        bs_embed, seq_len, _ = prompt_embeds.shape
        prompt_embeds = prompt_embeds.tile(
            repeat_times=[1, num_images_per_prompt, 1])
        prompt_embeds = prompt_embeds.reshape(
            [bs_embed * num_images_per_prompt, seq_len, -1])
        if do_classifier_free_guidance and negative_prompt_embeds is None:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [''] * batch_size
            elif prompt is not None and type(prompt) is not type(
                    negative_prompt):
                raise TypeError(
                    f'`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} != {type(prompt)}.'
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f'`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`: {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches the batch size of `prompt`.'
                )
            else:
                uncond_tokens = negative_prompt
            if isinstance(self, TextualInversionLoaderMixin):
                uncond_tokens = self.maybe_convert_prompt(uncond_tokens,
                                                          self.tokenizer)
            max_length = prompt_embeds.shape[1]
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding='max_length',
                max_length=max_length,
                truncation=True,
                return_tensors='pd')
            if hasattr(self.text_encoder.config, 'use_attention_mask'
                       ) and self.text_encoder.config.use_attention_mask:
                attention_mask = uncond_input.attention_mask
            else:
                attention_mask = None
            negative_prompt_embeds = self.text_encoder(
                uncond_input.input_ids, attention_mask=attention_mask)
            negative_prompt_embeds = negative_prompt_embeds[0]
        if do_classifier_free_guidance:
            seq_len = negative_prompt_embeds.shape[1]
            negative_prompt_embeds = negative_prompt_embeds.cast(
                dtype=self.text_encoder.dtype)
            negative_prompt_embeds = negative_prompt_embeds.tile(
                repeat_times=[1, num_images_per_prompt, 1])
            negative_prompt_embeds = negative_prompt_embeds.reshape(
                [batch_size * num_images_per_prompt, seq_len, -1])
            prompt_embeds = paddle.concat(
                x=[negative_prompt_embeds, prompt_embeds])
        return prompt_embeds

    def run_safety_checker(self, image, dtype):
        if self.safety_checker is None:
            has_nsfw_concept = None
        else:
            if paddle.is_tensor(x=image):
                feature_extractor_input = self.image_processor.postprocess(
                    image, output_type='pil')
            else:
                feature_extractor_input = self.image_processor.numpy_to_pil(
                    image)
            safety_checker_input = self.feature_extractor(
                feature_extractor_input, return_tensors='pd')
            image, has_nsfw_concept = self.safety_checker(
                images=image,
                clip_input=safety_checker_input.pixel_values.cast(dtype))
        return image, has_nsfw_concept

    def decode_latents(self, latents):
        warnings.warn(
            'The decode_latents method is deprecated and will be removed in a future version. Please use VaeImageProcessor instead',
            FutureWarning)
        latents = 1 / self.vae.config.scaling_factor * latents
        image = self.vae.decode(latents, return_dict=False)[0]
        image = (image / 2 + 0.5).clip(min=0, max=1)
        image = image.cpu().transpose(perm=[0, 2, 3, 1]).astype(
            dtype='float32').numpy()
        return image

    def prepare_extra_step_kwargs(self, generator, eta):
        accepts_eta = 'eta' in set(
            inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs['eta'] = eta
        accepts_generator = 'generator' in set(
            inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs['generator'] = generator
        return extra_step_kwargs

    def check_inputs(self,
                     prompt,
                     image,
                     callback_steps,
                     negative_prompt=None,
                     prompt_embeds=None,
                     negative_prompt_embeds=None,
                     controlnet_conditioning_scale=1.0,
                     control_guidance_start=0.0,
                     control_guidance_end=1.0):
        if callback_steps is None or callback_steps is not None and (
                not isinstance(callback_steps, int) or callback_steps <= 0):
            raise ValueError(
                f'`callback_steps` has to be a positive integer but is {callback_steps} of type {type(callback_steps)}.'
            )
        if prompt is not None and prompt_embeds is not None:
            raise ValueError(
                f'Cannot forward both `prompt`: {prompt} and `prompt_embeds`: {prompt_embeds}. Please make sure to only forward one of the two.'
            )
        elif prompt is None and prompt_embeds is None:
            raise ValueError(
                'Provide either `prompt` or `prompt_embeds`. Cannot leave both `prompt` and `prompt_embeds` undefined.'
            )
        elif prompt is not None and (not isinstance(prompt, str) and
                                     not isinstance(prompt, list)):
            raise ValueError(
                f'`prompt` has to be of type `str` or `list` but is {type(prompt)}'
            )
        if negative_prompt is not None and negative_prompt_embeds is not None:
            raise ValueError(
                f'Cannot forward both `negative_prompt`: {negative_prompt} and `negative_prompt_embeds`: {negative_prompt_embeds}. Please make sure to only forward one of the two.'
            )
        if prompt_embeds is not None and negative_prompt_embeds is not None:
            if prompt_embeds.shape != negative_prompt_embeds.shape:
                raise ValueError(
                    f'`prompt_embeds` and `negative_prompt_embeds` must have the same shape when passed directly, but got: `prompt_embeds` {prompt_embeds.shape} != `negative_prompt_embeds` {negative_prompt_embeds.shape}.'
                )
        if isinstance(self.controlnet, MultiControlNetModel):
            if isinstance(prompt, list):
                logger.warning(
                    f'You have {len(self.controlnet.nets)} ControlNets and you have passed {len(prompt)} prompts. The conditionings will be fixed across the prompts.'
                )

        if isinstance(self.controlnet, ControlNetModel):
            self.check_image(image, prompt, prompt_embeds)
        elif isinstance(self.controlnet, MultiControlNetModel):
            if not isinstance(image, list):
                raise TypeError(
                    'For multiple controlnets: `image` must be type `list`')
            elif any(isinstance(i, list) for i in image):
                raise ValueError(
                    'A single batch of multiple conditionings are supported at the moment.'
                )
            elif len(image) != len(self.controlnet.nets):
                raise ValueError(
                    f'For multiple controlnets: `image` must have the same length as the number of controlnets, but got {len(image)} images and {len(self.controlnet.nets)} ControlNets.'
                )
            for image_ in image:
                self.check_image(image_, prompt, prompt_embeds)
        else:
            assert False
        if isinstance(self.controlnet, ControlNetModel):
            if not isinstance(controlnet_conditioning_scale, float):
                raise TypeError(
                    'For single controlnet: `controlnet_conditioning_scale` must be type `float`.'
                )
        elif isinstance(self.controlnet, MultiControlNetModel):
            if isinstance(controlnet_conditioning_scale, list):
                if any(
                        isinstance(i, list)
                        for i in controlnet_conditioning_scale):
                    raise ValueError(
                        'A single batch of multiple conditionings are supported at the moment.'
                    )
            elif isinstance(controlnet_conditioning_scale, list) and len(
                    controlnet_conditioning_scale) != len(self.controlnet.nets):
                raise ValueError(
                    'For multiple controlnets: When `controlnet_conditioning_scale` is specified as `list`, it must have the same length as the number of controlnets'
                )
        else:
            assert False
        if len(control_guidance_start) != len(control_guidance_end):
            raise ValueError(
                f'`control_guidance_start` has {len(control_guidance_start)} elements, but `control_guidance_end` has {len(control_guidance_end)} elements. Make sure to provide the same number of elements to each list.'
            )
        if isinstance(self.controlnet, MultiControlNetModel):
            if len(control_guidance_start) != len(self.controlnet.nets):
                raise ValueError(
                    f'`control_guidance_start`: {control_guidance_start} has {len(control_guidance_start)} elements but there are {len(self.controlnet.nets)} controlnets available. Make sure to provide {len(self.controlnet.nets)}.'
                )
        for start, end in zip(control_guidance_start, control_guidance_end):
            if start >= end:
                raise ValueError(
                    f'control guidance start: {start} cannot be larger or equal to control guidance end: {end}.'
                )
            if start < 0.0:
                raise ValueError(
                    f"control guidance start: {start} can't be smaller than 0.")
            if end > 1.0:
                raise ValueError(
                    f"control guidance end: {end} can't be larger than 1.0.")

    def check_image(self, image, prompt, prompt_embeds):
        image_is_pil = isinstance(image, PIL.Image.Image)
        image_is_tensor = isinstance(image, paddle.Tensor)
        image_is_np = isinstance(image, np.ndarray)
        image_is_pil_list = isinstance(image, list) and isinstance(
            image[0], PIL.Image.Image)
        image_is_tensor_list = isinstance(image, list) and isinstance(
            image[0], paddle.Tensor)
        image_is_np_list = isinstance(image, list) and isinstance(image[0],
                                                                  np.ndarray)
        if (not image_is_pil and not image_is_tensor and not image_is_np and
                not image_is_pil_list and not image_is_tensor_list and
                not image_is_np_list):
            raise TypeError(
                f'image must be passed and be one of PIL image, numpy array, paddle tensor, list of PIL images, list of numpy arrays or list of paddle tensors, but is {type(image)}'
            )
        if image_is_pil:
            image_batch_size = 1
        else:
            image_batch_size = len(image)
        if prompt is not None and isinstance(prompt, str):
            prompt_batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            prompt_batch_size = len(prompt)
        elif prompt_embeds is not None:
            prompt_batch_size = prompt_embeds.shape[0]
        if image_batch_size != 1 and image_batch_size != prompt_batch_size:
            raise ValueError(
                f'If image batch size is not 1, image batch size must be same as prompt batch size. image batch size: {image_batch_size}, prompt batch size: {prompt_batch_size}'
            )

    def prepare_control_image(self,
                              image,
                              width,
                              height,
                              batch_size,
                              num_images_per_prompt,
                              dtype,
                              do_classifier_free_guidance=False,
                              guess_mode=False):
        image = self.control_image_processor.preprocess(
            image, height=height, width=width).cast(dtype='float32')
        image_batch_size = image.shape[0]
        if image_batch_size == 1:
            repeat_by = batch_size
        else:
            repeat_by = num_images_per_prompt
        image = image.repeat_interleave(repeats=repeat_by, axis=0)
        image = image.cast(dtype=dtype)
        if do_classifier_free_guidance and not guess_mode:
            image = paddle.concat(x=[image] * 2)
        return image

    def get_timesteps(self, num_inference_steps, strength):
        init_timestep = min(
            int(num_inference_steps * strength), num_inference_steps)
        t_start = max(num_inference_steps - init_timestep, 0)
        timesteps = self.scheduler.timesteps[t_start * self.scheduler.order:]
        return timesteps, num_inference_steps - t_start

    def prepare_latents(self,
                        image,
                        timestep,
                        batch_size,
                        num_images_per_prompt,
                        dtype,
                        generator=None):
        if not isinstance(image, (paddle.Tensor, PIL.Image.Image, list)):
            raise ValueError(
                f'`image` has to be of type `paddle.Tensor`, `PIL.Image.Image` or list but is {type(image)}'
            )
        image = image.cast(dtype=dtype)
        batch_size = batch_size * num_images_per_prompt
        if image.shape[1] == 4:
            init_latents = image
        else:
            if isinstance(generator, list) and len(generator) != batch_size:
                raise ValueError(
                    f'You have passed a list of generators of length {len(generator)}, but requested an effective batch size of {batch_size}. Make sure the batch size matches the length of the generators.'
                )
            elif isinstance(generator, list):
                init_latents = [
                    self.vae.encode(image[i:i + 1]).latent_dist.sample(
                        generator[i]) for i in range(batch_size)
                ]
                init_latents = paddle.concat(x=init_latents, axis=0)
            else:
                init_latents = self.vae.encode(image).latent_dist.sample(
                    generator)
            init_latents = self.vae.config.scaling_factor * init_latents
        if batch_size > init_latents.shape[
                0] and batch_size % init_latents.shape[0] == 0:
            deprecation_message = (
                f'You have passed {batch_size} text prompts (`prompt`), but only {init_latents.shape[0]} initial images (`image`). Initial images are now duplicating to match the number of text prompts. Note that this behavior is deprecated and will be removed in a version 1.0.0. Please make sure to update your script to pass as many initial images as text prompts to suppress this warning.'
            )
            deprecate(
                'len(prompt) != len(image)',
                '1.0.0',
                deprecation_message,
                standard_warn=False)
            additional_image_per_prompt = batch_size // init_latents.shape[0]
            init_latents = paddle.concat(
                x=[init_latents] * additional_image_per_prompt, axis=0)
        elif batch_size > init_latents.shape[
                0] and batch_size % init_latents.shape[0] != 0:
            raise ValueError(
                f'Cannot duplicate `image` of batch size {init_latents.shape[0]} to {batch_size} text prompts.'
            )
        else:
            init_latents = paddle.concat(x=[init_latents], axis=0)
        shape = init_latents.shape
        noise = randn_tensor(shape, generator=generator, dtype=dtype)
        init_latents = self.scheduler.add_noise(init_latents, noise, timestep)
        latents = init_latents
        return latents

    @paddle.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
            self,
            prompt: Union[str, List[str]]=None,
            image: Union[paddle.Tensor, PIL.Image.Image, np.ndarray, List[
                paddle.Tensor], List[PIL.Image.Image], List[np.ndarray]]=None,
            control_image: Union[paddle.Tensor, PIL.Image.Image, np.ndarray,
                                 List[paddle.Tensor], List[
                                     PIL.Image.Image], List[np.ndarray]]=None,
            height: Optional[int]=None,
            width: Optional[int]=None,
            strength: float=0.8,
            num_inference_steps: int=50,
            guidance_scale: float=7.5,
            negative_prompt: Optional[Union[str, List[str]]]=None,
            num_images_per_prompt: Optional[int]=1,
            eta: float=0.0,
            generator: Optional[Union[paddle.Generator, List[
                paddle.Generator]]]=None,
            latents: Optional[paddle.Tensor]=None,
            prompt_embeds: Optional[paddle.Tensor]=None,
            negative_prompt_embeds: Optional[paddle.Tensor]=None,
            output_type: Optional[str]='pil',
            return_dict: bool=True,
            callback: Optional[Callable[[int, int, paddle.Tensor], None]]=None,
            callback_steps: int=1,
            cross_attention_kwargs: Optional[Dict[str, Any]]=None,
            controlnet_conditioning_scale: Union[float, List[float]]=0.8,
            guess_mode: bool=False,
            control_guidance_start: Union[float, List[float]]=0.0,
            control_guidance_end: Union[float, List[float]]=1.0):
        """
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide the image generation. If not defined, one has to pass `prompt_embeds`.
                instead.
            image (`paddle.Tensor`, `PIL.Image.Image`, `np.ndarray`, `List[paddle.Tensor]`, `List[PIL.Image.Image]`, `List[np.ndarray]`,:
                    `List[List[paddle.Tensor]]`, `List[List[np.ndarray]]` or `List[List[PIL.Image.Image]]`):
                The initial image will be used as the starting point for the image generation process. Can also accpet
                image latents as `image`, if passing latents directly, it will not be encoded again.
            control_image (`paddle.Tensor`, `PIL.Image.Image`, `np.ndarray`, `List[paddle.Tensor]`, `List[PIL.Image.Image]`, `List[np.ndarray]`,:
                    `List[List[paddle.Tensor]]`, `List[List[np.ndarray]]` or `List[List[PIL.Image.Image]]`):
                The ControlNet input condition. ControlNet uses this input condition to generate guidance to Unet. If
                the type is specified as `paddle.Tensor`, it is passed to ControlNet as is. `PIL.Image.Image` can
                also be accepted as an image. The dimensions of the output image defaults to `image`'s dimensions. If
                height and/or width are passed, `image` is resized according to them. If multiple ControlNets are
                specified in init, images must be passed as a list such that each element of the list can be correctly
                batched for input to a single controlnet.
            height (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. If not defined, one has to pass
                `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
                less than `1`).
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`paddle.Generator` or `List[paddle.Generator]`, *optional*):
                One or a list of paddle generator(s) to make generation deterministic.
            latents (`paddle.Tensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            callback (`Callable`, *optional*):
                A function that will be called every `callback_steps` steps during inference. The function will be
                called with the following arguments: `callback(step: int, timestep: int, latents: paddle.Tensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function will be called. If not specified, the callback will be
                called at every step.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the `AttentionProcessor` as defined under
                `self.processor` in ppdiffusers.cross_attention.
            controlnet_conditioning_scale (`float` or `List[float]`, *optional*, defaults to 1.0):
                The outputs of the controlnet are multiplied by `controlnet_conditioning_scale` before they are added
                to the residual in the original unet. If multiple ControlNets are specified in init, you can set the
                corresponding scale as a list. Note that by default, we use a smaller conditioning scale for inpainting
                than for [`~StableDiffusionControlNetPipeline.__call__`].
            guess_mode (`bool`, *optional*, defaults to `False`):
                In this mode, the ControlNet encoder will try best to recognize the content of the input image even if
                you remove all prompts. The `guidance_scale` between 3.0 and 5.0 is recommended.
            control_guidance_start (`float` or `List[float]`, *optional*, defaults to 0.0):
                The percentage of total steps at which the controlnet starts applying.
            control_guidance_end (`float` or `List[float]`, *optional*, defaults to 1.0):
                The percentage of total steps at which the controlnet stops applying.

        Examples:

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
            When returning a tuple, the first element is a list with the generated images, and the second element is a
            list of `bool`s denoting whether the corresponding generated image likely represents "not-safe-for-work"
            (nsfw) content, according to the `safety_checker`.
        """
        controlnet = self.controlnet._orig_mod if is_compiled_module(
            self.controlnet) else self.controlnet
        if not isinstance(control_guidance_start, list) and isinstance(
                control_guidance_end, list):
            control_guidance_start = len(control_guidance_end) * [
                control_guidance_start
            ]
        elif not isinstance(control_guidance_end, list) and isinstance(
                control_guidance_start, list):
            control_guidance_end = len(control_guidance_start) * [
                control_guidance_end
            ]
        elif not isinstance(control_guidance_start, list) and not isinstance(
                control_guidance_end, list):
            mult = len(controlnet.nets) if isinstance(
                controlnet, MultiControlNetModel) else 1
            control_guidance_start, control_guidance_end = mult * [
                control_guidance_start
            ], mult * [control_guidance_end]
        self.check_inputs(prompt, control_image, callback_steps,
                          negative_prompt, prompt_embeds,
                          negative_prompt_embeds, controlnet_conditioning_scale,
                          control_guidance_start, control_guidance_end)
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]
        do_classifier_free_guidance = guidance_scale > 1.0
        controlnet = self.controlnet._orig_mod if is_compiled_module(
            self.controlnet) else self.controlnet
        if isinstance(controlnet, MultiControlNetModel) and isinstance(
                controlnet_conditioning_scale, float):
            controlnet_conditioning_scale = [controlnet_conditioning_scale
                                             ] * len(controlnet.nets)
        global_pool_conditions = (controlnet.config.global_pool_conditions
                                  if isinstance(controlnet, ControlNetModel)
                                  else controlnet.nets[0]
                                  .config.global_pool_conditions)
        guess_mode = guess_mode or global_pool_conditions
        text_encoder_lora_scale = cross_attention_kwargs.get(
            'scale', None) if cross_attention_kwargs is not None else None
        prompt_embeds = self._encode_prompt(
            prompt,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
            lora_scale=text_encoder_lora_scale)
        image = self.image_processor.preprocess(image).cast(dtype='float32')
        if isinstance(controlnet, ControlNetModel):
            control_image = self.prepare_control_image(
                image=control_image,
                width=width,
                height=height,
                batch_size=batch_size * num_images_per_prompt,
                num_images_per_prompt=num_images_per_prompt,
                dtype=controlnet.dtype,
                do_classifier_free_guidance=do_classifier_free_guidance,
                guess_mode=guess_mode)
        elif isinstance(controlnet, MultiControlNetModel):
            control_images = []
            for control_image_ in control_image:
                control_image_ = self.prepare_control_image(
                    image=control_image_,
                    width=width,
                    height=height,
                    batch_size=batch_size * num_images_per_prompt,
                    num_images_per_prompt=num_images_per_prompt,
                    dtype=controlnet.dtype,
                    do_classifier_free_guidance=do_classifier_free_guidance,
                    guess_mode=guess_mode)
                control_images.append(control_image_)
            control_image = control_images
        else:
            assert False
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps, num_inference_steps = self.get_timesteps(num_inference_steps,
                                                            strength)
        latent_timestep = timesteps[:1].tile(
            repeat_times=[batch_size * num_images_per_prompt])
        latents = self.prepare_latents(image, latent_timestep, batch_size,
                                       num_images_per_prompt,
                                       prompt_embeds.dtype, generator)
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
        controlnet_keep = []
        for i in range(len(timesteps)):
            keeps = [
                (1.0 - float(i / len(timesteps) < s or
                             (i + 1) / len(timesteps) > e))
                for s, e in zip(control_guidance_start, control_guidance_end)
            ]
            controlnet_keep.append(keeps[0] if isinstance(
                controlnet, ControlNetModel) else keeps)
        num_warmup_steps = len(
            timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                latent_model_input = paddle.concat(
                    x=[latents] * 2) if do_classifier_free_guidance else latents
                latent_model_input = self.scheduler.scale_model_input(
                    latent_model_input, t)
                if guess_mode and do_classifier_free_guidance:
                    control_model_input = latents
                    control_model_input = self.scheduler.scale_model_input(
                        control_model_input, t)
                    controlnet_prompt_embeds = prompt_embeds.chunk(chunks=2)[1]
                else:
                    control_model_input = latent_model_input
                    controlnet_prompt_embeds = prompt_embeds
                if isinstance(controlnet_keep[i], list):
                    cond_scale = [(c * s)
                                  for c, s in zip(controlnet_conditioning_scale,
                                                  controlnet_keep[i])]
                else:
                    cond_scale = (controlnet_conditioning_scale *
                                  controlnet_keep[i])
                down_block_res_samples, mid_block_res_sample = self.controlnet(
                    control_model_input,
                    t,
                    encoder_hidden_states=controlnet_prompt_embeds,
                    controlnet_cond=control_image,
                    conditioning_scale=cond_scale,
                    guess_mode=guess_mode,
                    return_dict=False)
                if guess_mode and do_classifier_free_guidance:
                    down_block_res_samples = [
                        paddle.concat(x=[paddle.zeros_like(x=d), d])
                        for d in down_block_res_samples
                    ]
                    mid_block_res_sample = paddle.concat(x=[
                        paddle.zeros_like(x=mid_block_res_sample),
                        mid_block_res_sample
                    ])
                noise_pred = self.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=prompt_embeds,
                    cross_attention_kwargs=cross_attention_kwargs,
                    down_block_additional_residuals=down_block_res_samples,
                    mid_block_additional_residual=mid_block_res_sample,
                    return_dict=False)[0]
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(
                        chunks=2)
                    noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond)
                latents = self.scheduler.step(
                    noise_pred,
                    t,
                    latents,
                    **extra_step_kwargs,
                    return_dict=False)[0]
                if i == len(timesteps) - 1 or i + 1 > num_warmup_steps and (
                        i + 1) % self.scheduler.order == 0:
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        callback(i, t, latents)
        if not output_type == 'latent':
            image = self.vae.decode(
                latents / self.vae.config.scaling_factor, return_dict=False)[0]
            image, has_nsfw_concept = self.run_safety_checker(
                image, prompt_embeds.dtype)
        else:
            image = latents
            has_nsfw_concept = None
        if has_nsfw_concept is None:
            do_denormalize = [True] * image.shape[0]
        else:
            do_denormalize = [(not has_nsfw) for has_nsfw in has_nsfw_concept]
        image = self.image_processor.postprocess(
            image, output_type=output_type, do_denormalize=do_denormalize)
        if not return_dict:
            return image, has_nsfw_concept
        return StableDiffusionPipelineOutput(
            images=image, nsfw_content_detected=has_nsfw_concept)