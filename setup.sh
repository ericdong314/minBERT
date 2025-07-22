#!/usr/bin/env bash

conda create -n cs224n_dfp_bert_39 python=3.9
conda activate cs224n_dfp_bert_39

pip install torch torchvision torchaudio
pip install tqdm==4.58.0
pip install requests==2.25.1
pip install importlib-metadata==3.7.0
pip install filelock==3.0.12
pip install sklearn==0.0
pip install tokenizers==0.15
pip install explainaboard_client==0.0.7
