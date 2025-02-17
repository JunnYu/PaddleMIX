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
import unittest

import numpy as np
import paddle

from ppdiffusers import (
    DDIMScheduler,
    KandinskyV22ControlnetPipeline,
    KandinskyV22PriorPipeline,
    UNet2DConditionModel,
    VQModel,
)
from ppdiffusers.utils import floats_tensor, load_image, load_numpy, slow
from ppdiffusers.utils.testing_utils import enable_full_determinism, require_paddle_gpu

from ..test_pipelines_common import PipelineTesterMixin, assert_mean_pixel_difference

enable_full_determinism()


class KandinskyV22ControlnetPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_class = KandinskyV22ControlnetPipeline
    params = ["image_embeds", "negative_image_embeds", "hint"]
    batch_params = ["image_embeds", "negative_image_embeds", "hint"]
    required_optional_params = [
        "generator",
        "height",
        "width",
        "latents",
        "guidance_scale",
        "num_inference_steps",
        "return_dict",
        "guidance_scale",
        "num_images_per_prompt",
        "output_type",
        "return_dict",
    ]

    @property
    def text_embedder_hidden_size(self):
        return 32

    @property
    def time_input_dim(self):
        return 32

    @property
    def block_out_channels_0(self):
        return self.time_input_dim

    @property
    def time_embed_dim(self):
        return self.time_input_dim * 4

    @property
    def cross_attention_dim(self):
        return 100

    @property
    def dummy_unet(self):
        paddle.seed(seed=0)
        model_kwargs = {
            "in_channels": 8,
            "out_channels": 8,
            "addition_embed_type": "image_hint",
            "down_block_types": ("ResnetDownsampleBlock2D", "SimpleCrossAttnDownBlock2D"),
            "up_block_types": ("SimpleCrossAttnUpBlock2D", "ResnetUpsampleBlock2D"),
            "mid_block_type": "UNetMidBlock2DSimpleCrossAttn",
            "block_out_channels": (self.block_out_channels_0, self.block_out_channels_0 * 2),
            "layers_per_block": 1,
            "encoder_hid_dim": self.text_embedder_hidden_size,
            "encoder_hid_dim_type": "image_proj",
            "cross_attention_dim": self.cross_attention_dim,
            "attention_head_dim": 4,
            "resnet_time_scale_shift": "scale_shift",
            "class_embed_type": None,
        }
        model = UNet2DConditionModel(**model_kwargs)
        return model

    @property
    def dummy_movq_kwargs(self):
        return {
            "block_out_channels": [32, 32, 64, 64],
            "down_block_types": [
                "DownEncoderBlock2D",
                "DownEncoderBlock2D",
                "DownEncoderBlock2D",
                "AttnDownEncoderBlock2D",
            ],
            "in_channels": 3,
            "latent_channels": 4,
            "layers_per_block": 1,
            "norm_num_groups": 8,
            "norm_type": "spatial",
            "num_vq_embeddings": 12,
            "out_channels": 3,
            "up_block_types": ["AttnUpDecoderBlock2D", "UpDecoderBlock2D", "UpDecoderBlock2D", "UpDecoderBlock2D"],
            "vq_embed_dim": 4,
        }

    @property
    def dummy_movq(self):
        paddle.seed(seed=0)
        model = VQModel(**self.dummy_movq_kwargs)
        return model

    def get_dummy_components(self):
        unet = self.dummy_unet
        movq = self.dummy_movq
        scheduler = DDIMScheduler(
            num_train_timesteps=1000,
            beta_schedule="linear",
            beta_start=0.00085,
            beta_end=0.012,
            clip_sample=False,
            set_alpha_to_one=False,
            steps_offset=1,
            prediction_type="epsilon",
            thresholding=False,
        )
        components = {"unet": unet, "scheduler": scheduler, "movq": movq}
        return components

    def get_dummy_inputs(self, seed=0):
        image_embeds = floats_tensor((1, self.text_embedder_hidden_size), rng=random.Random(seed))
        negative_image_embeds = floats_tensor((1, self.text_embedder_hidden_size), rng=random.Random(seed + 1))
        hint = floats_tensor((1, 3, 64, 64), rng=random.Random(seed))
        generator = paddle.Generator().manual_seed(seed)
        inputs = {
            "image_embeds": image_embeds,
            "negative_image_embeds": negative_image_embeds,
            "hint": hint,
            "generator": generator,
            "height": 64,
            "width": 64,
            "guidance_scale": 4.0,
            "num_inference_steps": 2,
            "output_type": "np",
        }
        return inputs

    def test_kandinsky_controlnet(self):

        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        output = pipe(**self.get_dummy_inputs())
        image = output.images
        image_from_tuple = pipe(**self.get_dummy_inputs(), return_dict=False)[0]
        image_slice = image[(0), -3:, -3:, (-1)]
        image_from_tuple_slice = image_from_tuple[(0), -3:, -3:, (-1)]
        assert image.shape == (1, 64, 64, 3)
        expected_slice = np.array([0.4625, 0.537, 1.0, 0.1296, 0.908, 1.0, 1.0, 1.0, 1.0])
        assert (
            np.abs(image_slice.flatten() - expected_slice).max() < 0.01
        ), f" expected_slice {expected_slice}, but got {image_slice.flatten()}"
        assert (
            np.abs(image_from_tuple_slice.flatten() - expected_slice).max() < 0.01
        ), f" expected_slice {expected_slice}, but got {image_from_tuple_slice.flatten()}"


@slow
@require_paddle_gpu
class KandinskyV22ControlnetPipelineIntegrationTests(unittest.TestCase):
    def tearDown(self):
        super().tearDown()
        gc.collect()
        paddle.device.cuda.empty_cache()

    def test_kandinsky_controlnet(self):
        expected_image = load_numpy(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/kandinskyv22/kandinskyv22_controlnet_robotcat_fp16.npy"
        )
        hint = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/kandinskyv22/hint_image_cat.png"
        )
        hint = paddle.to_tensor(data=np.array(hint)).astype(dtype="float32") / 255.0
        hint = hint.transpose(perm=[2, 0, 1]).unsqueeze(axis=0)
        pipe_prior = KandinskyV22PriorPipeline.from_pretrained(
            "kandinsky-community/kandinsky-2-2-prior", paddle_dtype="float16"
        )

        pipeline = KandinskyV22ControlnetPipeline.from_pretrained(
            "kandinsky-community/kandinsky-2-2-controlnet-depth", paddle_dtype="float16"
        )

        pipeline.set_progress_bar_config(disable=None)
        prompt = "A robot, 4k photo"
        generator = paddle.Generator().manual_seed(0)
        image_emb, zero_image_emb = pipe_prior(
            prompt, generator=generator, num_inference_steps=5, negative_prompt=""
        ).to_tuple()
        generator = paddle.Generator().manual_seed(0)
        output = pipeline(
            image_embeds=image_emb,
            negative_image_embeds=zero_image_emb,
            hint=hint,
            generator=generator,
            num_inference_steps=100,
            output_type="np",
        )
        image = output.images[0]
        assert image.shape == (512, 512, 3)
        assert_mean_pixel_difference(image, expected_image)
