"""Weight-training path for the trainable-model series (kind="weights" only).

The Claude Platform offers no fine-tuning of Claude models, so this path trains a separate model
behind an OpenAI-compatible fine-tuning API. Its results form their own series and are never pooled
with the Claude series into one causal claim. Prompt and few-shot improvements are different kinds
and are never produced here.

    dataset.py         teacher attempts -> supervised fine-tuning JSONL + manifest; equal-budget sampling
    backend.py         TrainingBackend protocol
    openai_backend.py  stdlib HTTP backend for the OpenAI-compatible fine-tuning API
    train.py           train(training_config, curriculum_ref, output_dir) -> training_ref, resumable
"""
