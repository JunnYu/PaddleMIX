#!/bin/bash

# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

source test_tipc/common_func.sh

FILENAME=$1

# MODE be one of ['lite_train_lite_infer' 'lite_train_whole_infer' 'whole_train_whole_infer',  
#                 'whole_infer', 'klquant_whole_infer',
#                 'cpp_infer', 'serving_infer']
# PaddleNLP supports 'lite_train_lite_infer', 'lite_train_whole_infer', 'whole_train_whole_infer' and 
# 'whole_infer' mode now.

MODE=$2

dataline=$(cat ${FILENAME})

# parser params
IFS=$'\n'
lines=(${dataline})

# The training params
model_name=$(func_parser_value "${lines[1]}")

trainer_list=$(func_parser_value "${lines[14]}")

if [ ${MODE} = "benchmark_train" ];then
    if [[ ${model_name} =~ "latent_diffusion_model" ]]; then
        rm -rf laion400m_demo_data.tar.gz
        rm -rf data
        wget https://paddlenlp.bj.bcebos.com/models/community/junnyu/develop/laion400m_demo_data.tar.gz
        tar -zxvf laion400m_demo_data.tar.gz
    fi

    if [[ ${model_name} =~ "stable_diffusion_model" ]]; then
        rm -rf laion400m_demo_data.tar.gz
        rm -rf data
        wget https://paddlenlp.bj.bcebos.com/models/community/junnyu/develop/laion400m_demo_data.tar.gz
        tar -zxvf laion400m_demo_data.tar.gz

        rm -rf CompVis-stable-diffusion-v1-4-paddle-init-pd.tar.gz
        rm -rf ./CompVis-stable-diffusion-v1-4-paddle-init
        wget https://bj.bcebos.com/paddlenlp/models/community/CompVis/CompVis-stable-diffusion-v1-4-paddle-init-pd.tar.gz
        tar -zxvf CompVis-stable-diffusion-v1-4-paddle-init-pd.tar.gz
    fi

    export PYTHONPATH=$(dirname "$PWD"):$PYTHONPATH
    python -m pip install --upgrade pip -i https://mirror.baidu.com/pypi/simple
    python -m pip install einops -i https://mirror.baidu.com/pypi/simple
    python -m pip install -r ../requirements.txt
    python -m pip install pybind11 regex sentencepiece tqdm visualdl attrdict easydict pyyaml -i https://mirror.baidu.com/pypi/simple

    # install develop paddlemix
    python -m pip install -e ../
    python -m pip list
    cd -
fi
