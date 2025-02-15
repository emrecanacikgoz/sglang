"""
Copyright 2023-2024 SGLang Team
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""A tensor parallel worker."""

import json
import logging
import threading
import time
from queue import Queue

import torch

from sglang.srt.configs.model_config import ModelConfig
from sglang.srt.hf_transformers_utils import get_processor, get_tokenizer
from sglang.srt.managers.io_struct import UpdateWeightReqInput
from sglang.srt.managers.schedule_batch import ModelWorkerBatch
from sglang.srt.model_executor.forward_batch_info import ForwardBatch
from sglang.srt.model_executor.model_runner import ModelRunner
from sglang.srt.server_args import ServerArgs
from sglang.srt.utils import broadcast_pyobj, is_multimodal_model, set_random_seed

logger = logging.getLogger(__name__)


class TpModelWorker:
    """A tensor parallel model worker."""

    def __init__(
        self,
        gpu_id: int,
        tp_rank: int,
        server_args: ServerArgs,
        nccl_port: int,
    ):
        # Parse args
        self.tp_rank = tp_rank

        # Init model and tokenizer
        self.model_config = ModelConfig(
            server_args.model_path,
            server_args.trust_remote_code,
            context_length=server_args.context_length,
            model_override_args=json.loads(server_args.json_model_override_args),
        )
        self.model_runner = ModelRunner(
            model_config=self.model_config,
            mem_fraction_static=server_args.mem_fraction_static,
            gpu_id=gpu_id,
            tp_rank=tp_rank,
            tp_size=server_args.tp_size,
            nccl_port=nccl_port,
            server_args=server_args,
        )
        if server_args.skip_tokenizer_init:
            self.tokenizer = self.processor = None
        else:
            if is_multimodal_model(self.model_config.hf_config.architectures):
                self.processor = get_processor(
                    server_args.tokenizer_path,
                    tokenizer_mode=server_args.tokenizer_mode,
                    trust_remote_code=server_args.trust_remote_code,
                )
                self.tokenizer = self.processor.tokenizer
            else:
                self.tokenizer = get_tokenizer(
                    server_args.tokenizer_path,
                    tokenizer_mode=server_args.tokenizer_mode,
                    trust_remote_code=server_args.trust_remote_code,
                )
        self.device = self.model_runner.device

        # Profile number of tokens
        self.max_total_num_tokens = self.model_runner.max_total_num_tokens
        self.max_prefill_tokens = server_args.max_prefill_tokens
        self.max_running_requests = min(
            (
                self.max_total_num_tokens // 2
                if server_args.max_running_requests is None
                else server_args.max_running_requests
            ),
            self.model_runner.req_to_token_pool.size,
        )
        self.max_req_input_len = min(
            self.model_config.context_len - 1,
            self.max_total_num_tokens - 1,
        )

        # Sync random seed across TP workers
        self.random_seed = broadcast_pyobj(
            [server_args.random_seed],
            self.tp_rank,
            self.model_runner.tp_group.cpu_group,
        )[0]
        set_random_seed(self.random_seed)

        if server_args.enable_overlap_schedule:
            self.init_overlap_status()

    def get_token_and_memory_info(self):
        return (
            self.max_total_num_tokens,
            self.max_prefill_tokens,
            self.max_running_requests,
            self.max_req_input_len,
            self.random_seed,
        )

    def init_overlap_status(self):
        self.future_logits_output_dict = dict()
        self.future_logits_output_ct = 0
        self.future_token_ids_ct = 0
        self.future_token_ids_map = torch.empty(
            (self.max_running_requests * 5,), dtype=torch.int32, device=self.device
        )
        self.future_token_ids_limit = self.max_running_requests * 3
        self.future_token_ids_output = dict()

        self.future_event_map = dict()
        self.forward_queue = Queue()
        self.forward_stream = torch.cuda.Stream()
        self.forward_thread = threading.Thread(
            target=self.forward_thread_func,
        )
        self.forward_thread.start()

    def forward_thread_func(self):
        with torch.cuda.stream(self.forward_stream):
            self.forward_thread_func_()

    @torch.inference_mode()
    def forward_thread_func_(self):
        while True:
            tic1 = time.time()
            model_worker_batch, future_logits_output, future_next_token_ids = (
                self.forward_queue.get()
            )

            # Resolve future tokens in the input
            tic2 = time.time()
            resolved_input_ids = model_worker_batch.input_ids
            future_mask = resolved_input_ids < 0
            resolved_input_ids[future_mask] = self.future_token_ids_map[
                -resolved_input_ids[future_mask]
            ]

            # Run forward
            logits_output, next_token_ids = self.forward_batch_generation(
                model_worker_batch
            )

            # Set future values
            if model_worker_batch.return_logprob:
                self.future_logits_output_dict[future_logits_output] = logits_output

            # logger.info(f"set output {future_next_token_ids=}, {next_token_ids=}")
            self.future_token_ids_map[-future_next_token_ids] = next_token_ids.to(
                torch.int32
            )
            # logger.info("Set event")
            self.future_token_ids_output[model_worker_batch.bid] = (
                next_token_ids.tolist()
            )
            self.future_event_map[model_worker_batch.bid].set()

            if False:
                tic3 = time.time()
                self.acc_time_with_waiting += tic3 - tic1
                self.acc_time_without_waiting += tic3 - tic2
                if self.forward_queue.qsize() == 0:
                    logger.info(
                        f"{self.acc_time_with_waiting=:.3f}, {self.acc_time_without_waiting=:.3f}, {self.forward_queue.qsize()=}"
                    )

    def resolve_future_token_ids(self, bid: int):
        self.future_event_map[bid].wait()
        ret = self.future_token_ids_output[bid]
        del self.future_event_map[bid]
        return ret

    def resolve_future_logits_output(self, future_obj):
        return self.future_logits_output_dict.pop(future_obj)

    def forward_batch_generation(self, model_worker_batch: ModelWorkerBatch):
        forward_batch = ForwardBatch.init_new(model_worker_batch, self.model_runner)
        logits_output = self.model_runner.forward(forward_batch)
        next_token_ids = self.model_runner.sample(logits_output, model_worker_batch)
        return logits_output, next_token_ids

    def forward_batch_embedding(self, model_worker_batch: ModelWorkerBatch):
        forward_batch = ForwardBatch.init_new(model_worker_batch, self.model_runner)
        logits_output = self.model_runner.forward(forward_batch)
        embeddings = logits_output.embeddings
        return embeddings

    def forward_batch_generation_non_blocking(
        self, model_worker_batch: ModelWorkerBatch
    ):
        # Allocate output future objects
        future_logits_output = self.future_logits_output_ct
        self.future_logits_output_ct += 1

        bs = len(model_worker_batch.seq_lens)
        with torch.cuda.stream(self.forward_stream):
            future_next_token_ids = -torch.arange(
                self.future_token_ids_ct + 1,
                self.future_token_ids_ct + 1 + bs,
                dtype=torch.int32,
                device=self.device,
            )
        self.future_token_ids_ct = (
            self.future_token_ids_ct + bs
        ) % self.future_token_ids_limit
        ret = future_logits_output, future_next_token_ids

        self.future_event_map[model_worker_batch.bid] = threading.Event()
        self.forward_queue.put(
            (model_worker_batch.copy(), future_logits_output, future_next_token_ids)
        )
        return ret

    def update_weights(self, recv_req: UpdateWeightReqInput):
        success, message = self.model_runner.update_weights(
            recv_req.model_path, recv_req.load_format
        )
        return success, message
