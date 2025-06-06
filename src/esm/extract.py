#!/usr/bin/env python3 -u
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import shutil
import argparse
import pathlib
import pandas as pd
import torch

from esm import Alphabet, FastaBatchedDataset, ProteinBertModel, pretrained, MSATransformer


def create_parser():
    parser = argparse.ArgumentParser(
        description="Extract per-token representations and model outputs for sequences in a FASTA file"  # noqa
    )

    parser.add_argument(
        #参数名，参数类型，描述信息
        "model_location",
        type=str,
        help="PyTorch model file OR name of pretrained model to download (see README for models)",
    )
    parser.add_argument(
        "fasta_file",
        type=pathlib.Path,
        help="FASTA file on which to extract representations",
    )
    parser.add_argument(
        "output_dir",
        type=pathlib.Path,
        help="output directory for extracted representations",
    )

    parser.add_argument(
        "--toks_per_batch",
          type=int, 
          default=4096, 
          help="maximum batch size")
    parser.add_argument(
        "--repr_layers",
        type=int,
        default=[-1],#倒数第一层
        nargs="+",
        help="layers indices from which to extract representations (0 to num_layers, inclusive)",
    )
    parser.add_argument(
        #nargs，接收参数数量，none，只能接受一个，？最多一个；*0个或者多个；+一个或者多个，并作为列表
        #choice取值范围
        #required，必须输入
        "--include",
        type=str,
        nargs="+",
        choices=["mean", "per_tok", "bos", "contacts"],#选一个
        help="specify which representations to return",
        required=True,
    )
    parser.add_argument(
        #加两个- -，选择型参数，，输入时为（-- truncation_seq_lengt 1022）
        "--truncation_seq_length",
        type=int,
        default=1022,
        help="truncate sequences longer than the given value",
    )

    parser.add_argument(
        #输入参数时--nogpu时，将会设为true
        "--nogpu", 
        action="store_true", 
        help="Do not use GPU even if available")

    parser.add_argument(
        "--concatenate_dir",
        type=pathlib.Path,
        default=None,
        help="output directory for concatenated representations",
    )

    return parser

def run(args):
    #下载模型和字母表
    model, alphabet = pretrained.load_model_and_alphabet(args.model_location)
    print("download over")
    model.eval()
    if isinstance(model, MSATransformer):
        raise ValueError(
            "This script currently does not handle models with MSA input (MSA Transformer)."
        )
    if torch.cuda.is_available() and not args.nogpu:#cuda可用 且 用户未使用“not gpu”
        model = model.cuda()
        print("Transferred model to GPU")
    #FastaBatchedDataset 是专门为处理 FASTA 文件而设计的数据集类，可能会对 FASTA 文件中的序列进行解析和预处理。
    dataset = FastaBatchedDataset.from_file(args.fasta_file)
    batches = dataset.get_batch_indices(args.toks_per_batch, extra_toks_per_seq=1)
    """
    torch.utils.data.DataLoader参数
    ataset (Dataset) – 加载数据的数据集。
    batch_size (int, optional) – 每个batch加载多少个样本(默认: 1)。
    shuffle (bool, optional) – 设置为True时会在每个epoch重新打乱数据(默认: False).
    sampler (Sampler, optional) – 定义从数据集中提取样本的策略，即生成index的方式，可以顺序也可以乱序
    num_workers (int, optional) – 用多少个子进程加载数据。0表示数据将在主进程中加载(默认: 0)
    collate_fn (callable, optional) –将一个batch的数据和标签进行合并操作。
    pin_memory (bool, optional) –设置pin_memory=True，则意味着生成的Tensor数据最开始是属于内存中的锁页内存，这样将内存的Tensor转义到GPU的显存就会更快一些。
    drop_last (bool, optional) – 如果数据集大小不能被batch size整除，则设置为True后可删除最后一个不完整的batch。如果设为False并且数据集的大小不能被batch size整除，则最后一个batch将更小。(默认: False)
    timeout，是用来设置数据读取的超时时间的，但超过这个时间还没读取到数据的话就会报错。
    """
    data_loader = torch.utils.data.DataLoader(
        dataset, 
        collate_fn=alphabet.get_batch_converter(), 
        batch_sampler=batches
    )
    print(f"Read {args.fasta_file} with {len(dataset)} sequences")

    #创建输出目录 args.output_dir的type是pathlib.path
    args.output_dir.mkdir(parents=True, exist_ok=True)

    return_contacts = "contacts" in args.include

    assert all(-(model.num_layers + 1) <= i <= model.num_layers for i in args.repr_layers)
    #取最后一层，reprlayers=[k]
    repr_layers = [(i + model.num_layers + 1) % (model.num_layers + 1) for i in args.repr_layers]

    with torch.no_grad():
        for batch_idx, (labels, strs, toks) in enumerate(data_loader):
            print(
                # 打印正在处理的批次信息，包括批次的索引和批次中的序列数量
                f"Processing {batch_idx + 1} of {len(batches)} batches ({toks.size(0)} sequences)"
            )
            if torch.cuda.is_available() and not args.nogpu:
                toks = toks.to(device="cuda", non_blocking=True)
                # 再次检查是否使用 GPU，如果使用，将 toks 转移到 GPU 上
            
            print(f"Device: {toks.device}")
                # 将批次数据 toks 输入到模型中，根据 repr_layers 和 return_contacts 参数获取模型的输出
            out = model(toks, repr_layers=repr_layers, return_contacts=return_contacts)

            logits = out["logits"].to(device="cpu")
            representations = {
                layer: t.to(device="cpu") for layer, t in out["representations"].items()
            }
            if return_contacts:
                contacts = out["contacts"].to(device="cpu")

            for i, label in enumerate(labels):
                args.output_file = args.output_dir / f"{label}.pt"
                args.output_file.parent.mkdir(parents=True, exist_ok=True)
                result = {"label": label}
                truncate_len = min(args.truncation_seq_length, len(strs[i]))
                # Call clone on tensors to ensure tensors are not views into a larger representation
                # See https://github.com/pytorch/pytorch/issues/1995
                if "per_tok" in args.include:
                    result["representations"] = {
                        layer: t[i, 1 : truncate_len + 1].clone()
                        for layer, t in representations.items()
                    }
                if "mean" in args.include:
                    result["mean_representations"] = {
                        layer: t[i, 1 : truncate_len + 1].mean(0).clone()
                        for layer, t in representations.items()
                    }
                if "bos" in args.include:
                    result["bos_representations"] = {
                        layer: t[i, 0].clone() for layer, t in representations.items()
                    }
                if return_contacts:
                    result["contacts"] = contacts[i, : truncate_len, : truncate_len].clone()
                

                #作为中间结果存储，便于后续cincatenate file将他们合并为便于处理的格式
                torch.save(
                    result,
                    args.output_file,
                )
                #print(f"pt文件存储在{args.output_file}")

    print(f"Saved representations to {args.output_dir}")

def concatenate_files(output_dir, output_csv):

    # Get all .pt files in the output directory
    files = []
    #walk：dirs,folders,files
    #join：合并目录和文件，自动加/
    for r, d, f in os.walk(output_dir):

        #遍历包括output在内的全部文件夹r，依次是output，a dir，b_dir；
        # 然后d是此文件夹内的文件夹名称；
        # 然后f是此文件下全部文件名，list存储

        #r全部文件夹名，包括otput_dir,每个文件夹哦都是str
        #f为全部文件夹输出list，内含全部文件名，带后缀
        #d,ouput文件夹下所有文件夹名称.list

        for file in f:
            if '.pt' in file:
                files.append(os.path.join(r, file))

    # Load each file and append to a list of dataframes
    dataframes = []
    for file_path in files:
        file_data = torch.load(file_path)
        #print(f"成功加载pt文件{file_path}")
        label = file_data['label']
        representations = file_data['mean_representations']
        key, tensor = representations.popitem()
        row_name = label
        row_data = tensor.tolist()
        new_df = pd.DataFrame([row_data], index=[row_name])
        dataframes.append(new_df)

    # Concatenate all dataframes
    if dataframes:
        concatenated_df = pd.concat(dataframes)
        print("Shape of concatenated DataFrame:", concatenated_df.shape)
        concatenated_df.to_csv(output_csv)
        print(f"Saved concatenated representations to {output_csv}")
    else:
        print("No data to concatenate.")

def main():
    parser = create_parser()
    args = parser.parse_args()
    
    run(args)

    if args.concatenate_dir is not None:
        fasta_file_name = args.fasta_file.stem
        output_csv = f"{args.concatenate_dir}/{fasta_file_name}_{args.model_location}.csv"
        concatenate_files(args.output_dir, output_csv)
        # print(f"Removing {args.output_dir}")
        # shutil.rmtree(args.output_dir)
    else:
        print("Skipping concatenation, file move, and cleanup as --concatenate_dir flag was not set.")

if __name__ == "__main__":
    main()