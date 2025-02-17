# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import gc
import random
import tempfile
import unittest

import numpy as np
import paddle
from paddlenlp.transformers import CLIPTextConfig, CLIPTextModel, CLIPTokenizer
from PIL import Image

from ppdiffusers import (
    AutoencoderKL,
    ControlNetModel,
    DDIMScheduler,
    StableDiffusionControlNetImg2ImgPipeline,
    UNet2DConditionModel,
)
from ppdiffusers.initializer import normal_, ones_
from ppdiffusers.pipelines.stable_diffusion.pipeline_stable_diffusion_controlnet import (
    MultiControlNetModel,
)
from ppdiffusers.utils import floats_tensor, load_image, load_numpy, randn_tensor, slow
from ppdiffusers.utils.testing_utils import enable_full_determinism, require_paddle_gpu

from ..pipeline_params import (
    IMAGE_TO_IMAGE_IMAGE_PARAMS,
    TEXT_GUIDED_IMAGE_VARIATION_BATCH_PARAMS,
    TEXT_GUIDED_IMAGE_VARIATION_PARAMS,
)
from ..test_pipelines_common import (
    PipelineKarrasSchedulerTesterMixin,
    PipelineLatentTesterMixin,
    PipelineTesterMixin,
)

enable_full_determinism()


class ControlNetImg2ImgPipelineFastTests(
    PipelineLatentTesterMixin, PipelineKarrasSchedulerTesterMixin, PipelineTesterMixin, unittest.TestCase
):
    pipeline_class = StableDiffusionControlNetImg2ImgPipeline
    params = TEXT_GUIDED_IMAGE_VARIATION_PARAMS - {"height", "width"}
    batch_params = TEXT_GUIDED_IMAGE_VARIATION_BATCH_PARAMS
    image_params = IMAGE_TO_IMAGE_IMAGE_PARAMS.union({"control_image"})
    image_latents_params = IMAGE_TO_IMAGE_IMAGE_PARAMS

    def get_dummy_components(self):
        paddle.seed(seed=0)
        unet = UNet2DConditionModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            sample_size=32,
            in_channels=4,
            out_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            up_block_types=("CrossAttnUpBlock2D", "UpBlock2D"),
            cross_attention_dim=32,
        )
        paddle.seed(seed=0)
        controlnet = ControlNetModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            in_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            cross_attention_dim=32,
            conditioning_embedding_out_channels=(16, 32),
        )
        paddle.seed(seed=0)
        scheduler = DDIMScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            clip_sample=False,
            set_alpha_to_one=False,
        )
        paddle.seed(seed=0)
        vae = AutoencoderKL(
            block_out_channels=[32, 64],
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D"],
            up_block_types=["UpDecoderBlock2D", "UpDecoderBlock2D"],
            latent_channels=4,
        )
        paddle.seed(seed=0)
        text_encoder_config = CLIPTextConfig(
            bos_token_id=0,
            eos_token_id=2,
            hidden_size=32,
            intermediate_size=37,
            layer_norm_eps=1e-05,
            num_attention_heads=4,
            num_hidden_layers=5,
            pad_token_id=1,
            vocab_size=1000,
        )
        text_encoder = CLIPTextModel(text_encoder_config)
        tokenizer = CLIPTokenizer.from_pretrained("hf-internal-testing/tiny-random-clip")
        components = {
            "unet": unet,
            "controlnet": controlnet,
            "scheduler": scheduler,
            "vae": vae,
            "text_encoder": text_encoder,
            "tokenizer": tokenizer,
            "safety_checker": None,
            "feature_extractor": None,
        }
        return components

    def get_dummy_inputs(self, seed=0):
        generator = paddle.Generator().manual_seed(seed)
        controlnet_embedder_scale_factor = 2
        control_image = randn_tensor(
            (1, 3, 32 * controlnet_embedder_scale_factor, 32 * controlnet_embedder_scale_factor),
            generator=generator,
        )
        image = floats_tensor(control_image.shape, rng=random.Random(seed))
        image = image.cpu().transpose(perm=[0, 2, 3, 1])[0]
        image = Image.fromarray(np.uint8(image)).convert("RGB").resize((64, 64))
        inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "generator": generator,
            "num_inference_steps": 2,
            "guidance_scale": 6.0,
            "output_type": "numpy",
            "image": image,
            "control_image": control_image,
        }
        return inputs

    def test_attention_slicing_forward_pass(self):
        return self._test_attention_slicing_forward_pass(expected_max_diff=0.002)

    def test_xformers_attention_forwardGenerator_pass(self):
        self._test_xformers_attention_forwardGenerator_pass(expected_max_diff=0.002)

    def test_inference_batch_single_identical(self):
        self._test_inference_batch_single_identical(expected_max_diff=0.002)


class StableDiffusionMultiControlNetPipelineFastTests(
    PipelineTesterMixin, PipelineKarrasSchedulerTesterMixin, unittest.TestCase
):
    pipeline_class = StableDiffusionControlNetImg2ImgPipeline
    params = TEXT_GUIDED_IMAGE_VARIATION_PARAMS - {"height", "width"}
    batch_params = TEXT_GUIDED_IMAGE_VARIATION_BATCH_PARAMS
    image_params = frozenset([])

    def get_dummy_components(self):
        paddle.seed(seed=0)
        unet = UNet2DConditionModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            sample_size=32,
            in_channels=4,
            out_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            up_block_types=("CrossAttnUpBlock2D", "UpBlock2D"),
            cross_attention_dim=32,
        )
        paddle.seed(seed=0)

        def init_weights(m):
            if isinstance(m, paddle.nn.Conv2D):
                normal_(m.weight)
                ones_(m.bias)

        controlnet1 = ControlNetModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            in_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            cross_attention_dim=32,
            conditioning_embedding_out_channels=(16, 32),
        )
        controlnet1.controlnet_down_blocks.apply(init_weights)

        paddle.seed(seed=0)
        controlnet2 = ControlNetModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            in_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            cross_attention_dim=32,
            conditioning_embedding_out_channels=(16, 32),
        )
        controlnet2.controlnet_down_blocks.apply(init_weights)

        paddle.seed(seed=0)
        scheduler = DDIMScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            clip_sample=False,
            set_alpha_to_one=False,
        )

        paddle.seed(seed=0)
        vae = AutoencoderKL(
            block_out_channels=[32, 64],
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D"],
            up_block_types=["UpDecoderBlock2D", "UpDecoderBlock2D"],
            latent_channels=4,
        )

        paddle.seed(seed=0)
        text_encoder_config = CLIPTextConfig(
            bos_token_id=0,
            eos_token_id=2,
            hidden_size=32,
            intermediate_size=37,
            layer_norm_eps=1e-05,
            num_attention_heads=4,
            num_hidden_layers=5,
            pad_token_id=1,
            vocab_size=1000,
        )
        text_encoder = CLIPTextModel(text_encoder_config)
        tokenizer = CLIPTokenizer.from_pretrained("hf-internal-testing/tiny-random-clip")
        controlnet = MultiControlNetModel([controlnet1, controlnet2])
        components = {
            "unet": unet,
            "controlnet": controlnet,
            "scheduler": scheduler,
            "vae": vae,
            "text_encoder": text_encoder,
            "tokenizer": tokenizer,
            "safety_checker": None,
            "feature_extractor": None,
        }
        return components

    def get_dummy_inputs(self, seed=0):
        generator = paddle.Generator().manual_seed(seed)
        controlnet_embedder_scale_factor = 2
        control_image = [
            randn_tensor(
                (1, 3, 32 * controlnet_embedder_scale_factor, 32 * controlnet_embedder_scale_factor),
                generator=generator,
            ),
            randn_tensor(
                (1, 3, 32 * controlnet_embedder_scale_factor, 32 * controlnet_embedder_scale_factor),
                generator=generator,
            ),
        ]
        image = floats_tensor(control_image[0].shape, rng=random.Random(seed))
        image = image.cpu().transpose(perm=[0, 2, 3, 1])[0]
        image = Image.fromarray(np.uint8(image)).convert("RGB").resize((64, 64))
        inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "generator": generator,
            "num_inference_steps": 2,
            "guidance_scale": 6.0,
            "output_type": "numpy",
            "image": image,
            "control_image": control_image,
        }
        return inputs

    def test_control_guidance_switch(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        scale = 10.0
        steps = 4
        inputs = self.get_dummy_inputs()
        inputs["num_inference_steps"] = steps
        inputs["controlnet_conditioning_scale"] = scale
        output_1 = pipe(**inputs)[0]
        inputs = self.get_dummy_inputs()
        inputs["num_inference_steps"] = steps
        inputs["controlnet_conditioning_scale"] = scale
        output_2 = pipe(**inputs, control_guidance_start=0.1, control_guidance_end=0.2)[0]
        inputs = self.get_dummy_inputs()
        inputs["num_inference_steps"] = steps
        inputs["controlnet_conditioning_scale"] = scale
        output_3 = pipe(**inputs, control_guidance_start=[0.1, 0.3], control_guidance_end=[0.2, 0.7])[0]
        inputs = self.get_dummy_inputs()
        inputs["num_inference_steps"] = steps
        inputs["controlnet_conditioning_scale"] = scale
        output_4 = pipe(**inputs, control_guidance_start=0.4, control_guidance_end=[0.5, 0.8])[0]
        assert np.sum(np.abs(output_1 - output_2)) > 0.001
        assert np.sum(np.abs(output_1 - output_3)) > 0.001
        assert np.sum(np.abs(output_1 - output_4)) > 0.001

    def test_attention_slicing_forward_pass(self):
        return self._test_attention_slicing_forward_pass(expected_max_diff=0.002)

    def test_xformers_attention_forwardGenerator_pass(self):
        self._test_xformers_attention_forwardGenerator_pass(expected_max_diff=0.002)

    def test_inference_batch_single_identical(self):
        self._test_inference_batch_single_identical(expected_max_diff=0.002)

    def test_save_pretrained_raise_not_implemented_exception(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                pipe.save_pretrained(tmpdir)
            except NotImplementedError:
                pass


@slow
@require_paddle_gpu
class ControlNetImg2ImgPipelineSlowTests(unittest.TestCase):
    def tearDown(self):
        super().tearDown()
        gc.collect()
        paddle.device.cuda.empty_cache()

    def test_canny(self):
        controlnet = ControlNetModel.from_pretrained("lllyasviel/sd-controlnet-canny")
        pipe = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
            "runwayml/stable-diffusion-v1-5", safety_checker=None, controlnet=controlnet
        )
        # pipe.enable_model_cpu_offload()
        pipe.set_progress_bar_config(disable=None)
        generator = paddle.Generator().manual_seed(0)
        prompt = "evil space-punk bird"
        control_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/sd_controlnet/bird_canny.png"
        ).resize((512, 512))
        image = load_image(
            "https://huggingface.co/lllyasviel/sd-controlnet-canny/resolve/main/images/bird.png"
        ).resize((512, 512))
        output = pipe(
            prompt,
            image,
            control_image=control_image,
            generator=generator,
            output_type="np",
            num_inference_steps=50,
            strength=0.6,
        )
        image = output.images[0]
        assert image.shape == (512, 512, 3)
        expected_image = load_numpy(
            "https://bj.bcebos.com/v1/paddlenlp/models/community/hf-internal-testing/sd_controlnet/img2img.np"
        )
        assert np.abs(expected_image - image).max() < 0.09

    def test_load_local(self):
        controlnet = ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_canny")
        pipe_1 = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
            "runwayml/stable-diffusion-v1-5", safety_checker=None, controlnet=controlnet
        )
        controlnet = ControlNetModel.from_single_file(
            "https://huggingface.co/lllyasviel/ControlNet-v1-1/blob/main/control_v11p_sd15_canny.pth"
        )
        pipe_2 = StableDiffusionControlNetImg2ImgPipeline.from_single_file(
            "https://huggingface.co/runwayml/stable-diffusion-v1-5/blob/main/v1-5-pruned-emaonly.safetensors",
            safety_checker=None,
            controlnet=controlnet,
        )
        control_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/sd_controlnet/bird_canny.png"
        ).resize((512, 512))
        image = load_image(
            "https://huggingface.co/lllyasviel/sd-controlnet-canny/resolve/main/images/bird.png"
        ).resize((512, 512))
        pipes = [pipe_1, pipe_2]
        images = []
        for pipe in pipes:
            pipe.enable_model_cpu_offload()
            pipe.set_progress_bar_config(disable=None)
            generator = paddle.Generator().manual_seed(0)
            prompt = "bird"
            output = pipe(
                prompt,
                image=image,
                control_image=control_image,
                strength=0.9,
                generator=generator,
                output_type="np",
                num_inference_steps=3,
            )
            images.append(output.images[0])
            del pipe
            gc.collect()
            paddle.device.cuda.empty_cache()
        assert np.abs(images[0] - images[1]).sum() < 0.001
