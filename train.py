from transformers import (
    set_seed,
    HfArgumentParser,
    TrainingArguments,
)
import argparse
from loguru import logger
import os
from os.path import join
import torch
from transformers import AutoTokenizer
from transformers import AutoModelForCausalLM
from transformers.integrations import is_deepspeed_zero3_enabled
from component.collator import SFTDataCollator, PretrainCollator
from component.dataset import (
    SFTDataset,
    ChatGLM2SFTDataset,
    ChatGLM3SFTDataset,
    MistralSFTDataset,
    ZephyrSFTDataset,
    QwenSFTDataset,
    PretrainDataset,
    LazyPretrainDataset
)
from component.argument import CustomizedArguments
from component.trainer import Trainer
# from component.loss import TargetLMLoss


def setup_everything():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train_args_file", type=str, default='train_args/full/qwen-7b-sft-full.json', help="")
    parser.add_argument("--local_rank", type=int, help="")
    args = parser.parse_args()
    train_args_file = args.train_args_file
    # train_args_file = 'train_args/finetune.json'
    # 读取训练的参数配置
    parser = HfArgumentParser((CustomizedArguments, TrainingArguments))
    # 解析得到自定义参数，以及自带参数
    args, training_args = parser.parse_json_file(json_file=train_args_file)
    # 创建输出目录
    if not os.path.exists(training_args.output_dir):
        os.makedirs(training_args.output_dir)
    logger.add(join(training_args.output_dir, 'train.log'))
    logger.info("train_args:{}".format(training_args))
    # 设置随机种子
    set_seed(training_args.seed)
    return args, training_args


def init_components(args, training_args):
    """
    初始化各个组件
    """
    logger.info('Initializing components...')
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    ddp = world_size != 1
    if ddp:
        device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
    training_args.ddp_find_unused_parameters = False if ddp else None

    # 初始化model
    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        torch_dtype=torch.float16,
        device_map=device_map if not is_deepspeed_zero3_enabled() else None,
        low_cpu_mem_usage=(not is_deepspeed_zero3_enabled()),
        trust_remote_code=True
    )
    # moe模型，需要考虑负载均衡的loss
    if 'output_router_logits' in model.config.to_dict():
        logger.info('set output_router_logits as True')
        model.config.output_router_logits = True

    # 加载tokenzier
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path,
        trust_remote_code=True,
        # llama不支持fast
        use_fast=False if model.config.model_type == 'llama' else True
    )
    # QWenTokenizer比较特殊，pad_token_id、bos_token_id、eos_token_id均为None。eod_id对应的token为<|endoftext|>
    if tokenizer.__class__.__name__ == 'QWenTokenizer':
        tokenizer.pad_token_id = tokenizer.eod_id
        tokenizer.bos_token_id = tokenizer.eod_id
        tokenizer.eos_token_id = tokenizer.eod_id
    # ChatGLMTokenizer不需要设置，仅设置其他tokenizer
    elif tokenizer.__class__.__name__ != 'ChatGLMTokenizer':
        assert tokenizer.eos_token_id is not None
        assert tokenizer.bos_token_id is not None
        tokenizer.pad_token_id = tokenizer.eos_token_id if tokenizer.pad_token_id is None else tokenizer.pad_token_id

    # 计算模型参数量
    total = sum(p.numel() for p in model.parameters())
    logger.info("Total model params: %.2fM" % (total / 1e6))

    # 初始化损失函数
    # loss_func = TargetLMLoss(ignore_index=-100)

    assert args.task_type in ['sft', 'pretrain'], 'task_type should be in [sft, pretrain]'
    # 初始化dataset和collator
    # 预训练
    if args.task_type == 'pretrain':
        train_dataset = LazyPretrainDataset(
            args.train_file, tokenizer, args.max_seq_length,args.tokenize_num_workers
        )
        data_collator = PretrainCollator(tokenizer, args.max_seq_length)
    else:
        # 指令微调，不同的模型，数据拼接格式不一样
        if 'chatglm2' in args.model_name_or_path.lower():
            train_dataset = ChatGLM2SFTDataset(args.train_file, tokenizer, args.max_seq_length)
        # 加载ChatGLM3的训练集
        elif 'chatglm3' in args.model_name_or_path.lower():
            train_dataset = ChatGLM3SFTDataset(args.train_file, tokenizer, args.max_seq_length)
        elif 'mistral' in args.model_name_or_path.lower() or 'mixtral' in args.model_name_or_path.lower():
            train_dataset = MistralSFTDataset(args.train_file, tokenizer, args.max_seq_length)
        elif 'zephyr' in args.model_name_or_path.lower():
            train_dataset = ZephyrSFTDataset(args.train_file, tokenizer, args.max_seq_length)
        elif 'qwen' in args.model_name_or_path.lower():
            train_dataset = QwenSFTDataset(args.train_file, tokenizer, args.max_seq_length)
        # 按照firefly格式进行拼接
        else:
            train_dataset = SFTDataset(args.train_file, tokenizer, args.max_seq_length)
        # 加载collator
        data_collator = SFTDataCollator(tokenizer, args.max_seq_length)

    # 初始化Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        # tokenizer=tokenizer,
        data_collator=data_collator,
        # compute_loss=loss_func
    )
    return trainer


def main():
    # 进行一些配置和检查
    args, training_args = setup_everything()
    # 加载各种组件
    trainer = init_components(args, training_args)
    # 开始训练
    logger.info("*** starting training ***")
    train_result = trainer.train()
    # 保存最好的checkpoint
    final_save_path = join(training_args.output_dir)
    trainer.save_model(final_save_path)  # Saves the tokenizer too
    # 保存训练指标
    metrics = train_result.metrics
    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    trainer.save_state()


if __name__ == "__main__":
    main()
