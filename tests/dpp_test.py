# Copyright (C) 2021 THL A29 Limited, a Tencent company.
# All rights reserved.
# Licensed under the BSD 3-Clause License (the "License"); you may
# not use this file except in compliance with the License. You may
# obtain a copy of the License at
# https://opensource.org/licenses/BSD-3-Clause
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.
# See the AUTHORS file for names of contributors.

import os
import json
import argparse
import torch
from torch.utils.data import SequentialSampler
import torch.optim as optim
import logging
import time
import torch.distributed as dist

from ops import TorchAdam, FP16Adam
from client import PatrickStarClient, setup_patrickstar_hooks, PSTensorStatus
from patrickstar.utils import see_memory_usage
import patrickstar.utils.global_timer as global_timer

from fp16 import configure_fp16_optimizer
from fp16 import FP16_Module
from fp16 import FP16_Optimizer

from tests.simple_net import SimpleModel, get_bert_data_loader
from runtime import initialize_engine, Init
from deepspeed_helper.global_vars import set_global_variables
from deepspeed_helper.global_vars import get_args
from manager import PatrickStarManager


def test_simple_model(is_ps: bool = False,
                      is_fp16: bool = False,
                      is_ckp: bool = True,
                      stop_iter: int = 10):
    logging.info(f'test a simple model with hybrid ps {is_ps} FP16 {is_fp16}')
    args = get_args()

    hidden_dim = 4
    batch_size = 4

    if not torch.distributed.is_initialized():
        dist.init_process_group(
            backend='gloo' if args.use_fake_dist else 'nccl')

    if args.use_fake_dist:
        rank = 0
    else:
        rank = args.local_rank

    world_size = torch.distributed.get_world_size()

    torch.cuda.set_device(rank)
    device = torch.device(f'cuda:{rank}')

    lr = 0.001
    betas = (0.9, 0.999)
    eps = 1e-6
    weight_decay = 0

    if not is_ps:
        model = SimpleModel(hidden_dim, is_ckp=is_ckp)
        model.cuda(rank)

        if is_fp16:
            model = FP16_Module(model)
        model.train()
        optimizer = TorchAdam(model.parameters(),
                              lr=lr,
                              betas=betas,
                              eps=eps,
                              weight_decay=weight_decay)
        if is_fp16:
            optimizer = FP16_Optimizer(optimizer)
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[rank])
    else:
        if is_fp16:
            client = PatrickStarClient(
                rank=rank,
                default_chunk_size=args.default_chunk_size,
                warmup=True,
                is_fp16=True)

            with Init(dtype=torch.float, client=client):
                model = SimpleModel(hidden_dim,
                                    is_ckp=is_ckp,
                                    use_cpu_embedding=args.use_cpu_embedding)

            model, optimizer, _, _ = initialize_engine(
                args=None,
                model=model,
                client=client,
                model_parameters=model.parameters(),
                lr=lr,
                betas=betas,
                eps=eps,
                weight_decay=weight_decay)

    see_memory_usage(f"PS {is_ps} after model init", force=True)

    data_loader = get_bert_data_loader(batch_size=batch_size,
                                       total_samples=100000,
                                       sequence_length=10,
                                       device=device,
                                       is_distrbuted=True)

    loss_res = []

    if is_ps:
        mgr = PatrickStarManager()
        mgr.start_train(is_warmup=True)

    start_time = time.time()
    for n, batch in enumerate(data_loader):
        loss = model(batch[0], batch[1])

        print(f"LOSS: {loss.item()} at {n}")
        loss_res.append(loss.item())

        if not is_ps:
            if is_fp16:
                optimizer.zero_grad(set_grads_to_None=True)
                optimizer.backward(loss, update_master_grads=False)
                optimizer.update_master_grads()
            else:
                optimizer.zero_grad()
                loss.backward()
        else:
            if is_fp16:
                model.backward(loss)
            else:
                optimizer.zero_grad()
                loss.backward()

        optimizer.step()

        see_memory_usage(f"PS {is_ps} after step {n}", force=True)

        if is_ps:
            global_timer.my_timer.print()
            global_timer.my_timer.reset()
        if n == stop_iter: break

    elapse = time.time() - start_time
    logging.info(f"is_ps {is_ps} elapse {elapse}")
    logging.info("======================" * 4)

    return loss_res


if __name__ == "__main__":
    logging.basicConfig(
        format=
        '%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d:%H:%M:%S',
        level=logging.INFO)
    set_global_variables()

    torch.manual_seed(0)
    # 4 layer每层20个elem(20*4 bytes)，最少360 (360*4 bytes)内存
    # gpu内存至少为40，反向传播一层需要的最大内存。

    test_cpu_adam = False
    if test_cpu_adam:
        loss_ref_list = test_simple_model(False)

        torch.manual_seed(0)
        loss_list = test_simple_model(True)

        print('hybridps', loss_list)
        print('ref', loss_ref_list)
        for loss, loss_ref in zip(loss_list, loss_ref_list):
            assert loss == loss_ref

    test_fp16 = True
    if test_fp16:
        # hidden_dim = 4
        # 需要 40和8两个chunk

        torch.manual_seed(0)
        loss_list = test_simple_model(is_ps=False, is_fp16=True, is_ckp=True)
        see_memory_usage("after PatrickStar simple model", force=True)

        torch.manual_seed(0)
        loss_list_ref = test_simple_model(is_ps=True,
                                          is_fp16=True,
                                          is_ckp=True)

        print('ps loss', loss_list)
        print('ref loss', loss_list_ref)

        import numpy as np
        print('diff ', np.array(loss_list) - np.array(loss_list_ref))
        for loss, loss_ref in zip(loss_list, loss_list_ref):
            assert loss == loss_ref, f"{loss - loss_ref}"

# embeddings.bert_embedding.word_embeddings.weight
# embeddings.bert_embedding.position_embeddings.weight
# embeddings.bert_embedding.token_type_embeddings.weight
# embeddings.bert_embedding.LayerNorm.weight
# embeddings.bert_embedding.LayerNorm.bias
# encoder.linear1.0.weight
# encoder.linear1.0.bias
# encoder.linear1.1.weight
# encoder.linear1.1.bias
# encoder.linear1.2.weight
# encoder.linear1.2.bias
# encoder.linear3.weight
# encoder.linear3.bias
# encoder.linear4.weight
# encoder.linear4.bias
# encoder.linear5.weight
# encoder.linear5.bias
