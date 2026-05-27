# Copyright 2025 Bytedance Ltd. and/or its affiliates
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

import uuid
import asyncio
import time
import os
import re
import numpy as np
import torch
import ray
import gc

from typing import Optional, List, Tuple, Dict, Union
from dataclasses import dataclass, field
from ray.util import ActorPool
from tqdm import tqdm
from loguru import logger
from vllm import AsyncEngineArgs, AsyncLLMEngine, SamplingParams
from lean_dojo import (
    Theorem,
    Dojo,
    DojoCrashError,
    TacticState,
    TacticResult,
)

from proof_tree import (
    InternalNode,
    ProofFinishedNode,
    ErrorNode,
    Edge,
    Status,
    LeanError,
    ProofFinished,
    ProofGivenUp,
    validate_proof,
    extract_proof_data,
)
from process_utils import clean_old_dojo, run_tac_cpu_timeout, LeanCPUTimedOut, DojoRestartRequired


def _pp1(self) -> str:
    head, *_ = self.pp.split("\n\n")
    pp1 = re.sub(r'^\s*case .*\n?', "", head).strip()
    pp1 = re.sub(r'^\s*this.*? : True\n?', "", pp1, flags=re.MULTILINE)
    return pp1

TacticState.pp1 = property(_pp1)

Node = Union[InternalNode, ProofFinishedNode, ErrorNode]

LOG_RATE = float(os.environ.get("LOG_RATE", 0.01))
LOG_RATE_FREQUENT_EVENT = float(os.environ.get("LOG_RATE_FREQUENT_EVENT", 0.001))
MODEL_READINESS_TIMEOUT = int(os.environ.get("MODEL_READINESS_TIMEOUT", 600))
logger.info(f"LOG_RATE={LOG_RATE}")

@dataclass
class ProofTask:
    """A dataclass to bundle a theorem with its plan for a search task."""
    theorem: Theorem
    plan: Optional[List[str]] = None

@dataclass(frozen=True)
class SearchResult:
    url: str
    commit: str
    full_name: str
    file_path: str
    status: Status
    proof: Optional[Tuple[Tuple[str, str]]]
    total_attempts: int

    # profiling info
    tactic_time: float
    vllm_time: float
    total_time: float
    total_nodes: int
    explored_nodes: int

    # preference pairs
    preference_pairs: Optional[Tuple[Tuple[str, str, str]]] = field(default=None)

    # proof_stats: time & logp for each corresponding tactic: List[(time, logp)]
    proof_stats: Optional[Tuple[Tuple[float, float]]] = field(default=None)

    # proof validation result
    proof_validation_passed: Optional[bool] = field(default=None)

    def __str__(self) -> str:
        max_proof_tactic_time = (
            max(t for t, _ in self.proof_stats) if self.proof_stats else 0.0
        )
        return (
            f"SearchResult(url={self.url}, commit={self.commit}, "
            f"full_name={self.full_name}, file_path={self.file_path}, "
            f"status={self.status}, "
            f"tactics={[tactic for _, tactic in self.proof] if self.proof else None}, "
            f"tactic_time={self.tactic_time:.2f}s, "
            f"max_proof_tactic_time={max_proof_tactic_time:.2f}s, "
            f"vllm_time={self.vllm_time:.2f}s, "
            f"total_time={self.total_time:.2f}s, "
            f"total_nodes={self.total_nodes}, "
            f"explored_nodes={self.explored_nodes}, "
            f"total_attempts={self.total_attempts}, "
            f"proof_validation_passed={self.proof_validation_passed})"
        )


@ray.remote
class TacticGenerator:

    def __init__(
        self,
        model_path: str,
        max_num_batched_tokens: int = 8192,
        max_num_seqs: int = 256,
    ) -> None:
        self.num_gpus = len(ray.get_gpu_ids())
        engine_args = AsyncEngineArgs(
            model=model_path,
            max_num_batched_tokens=max_num_batched_tokens,
            enable_chunked_prefill=True,
            pipeline_parallel_size=1,
            tensor_parallel_size=self.num_gpus,
            max_num_seqs=max_num_seqs,
            max_model_len=16384,
            disable_custom_all_reduce=True,
        )
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        self._is_ready = True  # Mark as ready after initialization
        logger.info(
            f"TacticGenerator initialized successfully, num_gpus={self.num_gpus}"
        )
    async def is_ready(self) -> bool:
        if not (hasattr(self, "_is_ready") and self._is_ready):
            return False
        if not (hasattr(self, "engine") and self.engine):
            return False
        try:
            await self.engine.check_health()   # No exception means OK
            return True
        except Exception as e:
            logger.info(
                f"TacticGenerator check_health failed, {e}"
            )
            return False
        # return hasattr(self, "_is_ready") and self._is_ready and hasattr(self, "engine")

    async def generate(
        self, tactic_state: str, sampling_params: SamplingParams
    ) -> List[Tuple[str, float]]:
        # Only final output is needed, as it has the complete generated text
        async for oup in self.engine.generate(
            tactic_state + ":::", sampling_params, request_id=str(uuid.uuid4().hex)
        ):
            final_output = oup

        suggestions = [
            (x.text.strip(), x.cumulative_logprob / len(x.token_ids) if len(x.token_ids) > 0 else x.cumulative_logprob) for x in final_output.outputs
        ]

        buggy_keywords = ["rcases", "cases'", "simpa"]

        filtered_suggestions = [
            (text, score)
            for text, score in suggestions
            if "sorry" not in text
            and "admit" not in text
            and "native_decide" not in text
            and not (any(keyword in text for keyword in buggy_keywords) and "?_" in text)
            and not ("simpa" in text and (" _" in text or "_ " in text or "_," in text or ",_" in text))
        ]

        return filtered_suggestions


class TacticManager:
    """
    A class that handles tactic generation and execution for the theorem proving system.
    """

    def __init__(
        self,
        tac_gen: TacticGenerator,
        dojo: Dojo,
        has_plan: bool,
        sampling_params: SamplingParams,
        vllm_timeout: int = 60,
        tactic_timeout: int = 10
    ) -> None:
        """
        Initialize the TacticManager, that handles tactic execution and edge generation.

        Args:
            tac_gen: The tactic generator actor/service
            dojo: The Lean Dojo instance
            has_plan: Whether the theorem comes with a plan
            sampling_params: Parameters for the sampling process
            vllm_timeout: Maximum time (in seconds) to wait for vLLM generation
            tactic_timeout: Maximum time (in seconds) to wait for tactic execution
        """
        self.tac_gen = tac_gen
        self.dojo = dojo
        self.has_plan = has_plan
        self.sampling_params = sampling_params
        self.vllm_timeout = vllm_timeout
        self.tactic_timeout = tactic_timeout
        self.started_at    = time.monotonic()

        # Profiling metrics
        self.tactic_time = 0.0
        self.vllm_time = 0.0

    async def generate_tactics(self, tactic_state: str) -> List[Tuple[str, float]]:
        """
        Generate a list of tactics with their log probabilities for a given tactic state.
        """
        vllm_start = time.time()
        try:
            suggestions = await asyncio.wait_for(
                self.tac_gen.generate.remote(
                    tactic_state=tactic_state, sampling_params=self.sampling_params
                ),
                timeout=self.vllm_timeout,
            )
        except asyncio.TimeoutError:
            raise ValueError(
                f"VLLM timed out after {time.time() - vllm_start:.2f}s for state {tactic_state}"
            )
        self.vllm_time += time.time() - vllm_start

        if not suggestions:
            raise ValueError(f"No suggestions generated for state {tactic_state}")

        # Dedup generated tactics
        seen = set()
        dedupped_suggestions = []
        for tactic, logprob in suggestions:
            if tactic not in seen:
                seen.add(tactic)
                dedupped_suggestions.append((tactic, logprob))
        if np.random.rand() < LOG_RATE:
            logger.info(
                f"Generated {len(dedupped_suggestions)} unique tactics: {dedupped_suggestions}"
            )
        suggestions = dedupped_suggestions
        return suggestions

    async def run_tactic(self, node, tactic, cached: bool = False):
        wall_start = time.time()
        try:
            response = await run_tac_cpu_timeout(
                self.dojo, node.state, tactic,
                focus_mode=self.has_plan,
                cpu_sec_limit=self.tactic_timeout,
                wall_sec_limit=self.tactic_timeout * 1.5,
            )
        except LeanCPUTimedOut as e:
            msg = (f"{e.limit_type}-timeout "           # Use unified message format
                f"(cpu={e.cpu_elapsed:.3f}s, wall={e.wall_elapsed:.3f}s, "
                f"limit={e.limit_value}s)")
            logger.warning(f"Tactic {tactic} -> {msg}, cached={cached}") # Log directly
            response = LeanError(msg)

            if e.killed_hard:
                try:
                    clean_old_dojo(self.dojo)
                except Exception as ex:
                    logger.warning(f"Failed to clean dojo after hard-timeout: {ex}")
                finally:
                    gc.collect()
                raise DojoRestartRequired("Dojo hard-timeout; request top-level restart")

        except DojoCrashError:
            response = LeanError(f"DojoCrashError, cached={cached}")

        except Exception as ex:
            response = LeanError(f"Tactic exec exception: {ex}, cached={cached}")
            if np.random.rand() < LOG_RATE:
                logger.warning(f"Tactic exec exception {ex}: {tactic}, cached={cached}")

        exec_time = time.time() - wall_start
        self.tactic_time += exec_time
        return response, exec_time

    def create_edge(
        self,
        src_node: InternalNode,
        tactic: str,
        time: float,
        logprob: float,
        response: Optional[TacticResult] = None,
        dst_node: Optional[Node] = None,
    ) -> Edge:
        current_depth = src_node.depth

        if dst_node is None and response is None:
            raise ValueError("Either response or dst_node must be provided")

        elif dst_node is not None and response is not None:
            raise ValueError(
                "Response and dst_node should not be provided at the same time"
            )

        if dst_node is None:
            assert response is not None, "Response must be provided if dst_node is None"

            if isinstance(response, ProofFinished) or isinstance(response, ProofGivenUp):
                dst_node = ProofFinishedNode(response, depth=current_depth + 1)

            elif isinstance(response, LeanError):
                dst_node = ErrorNode(response, depth=current_depth + 1)

            elif isinstance(response, TacticState):
                dst_node = InternalNode(
                    state=response,
                    cumulative_logprob=logprob + src_node.cumulative_logprob,
                    depth=current_depth + 1,
                )

            else:
                raise ValueError(f"Unknown response type: {type(response)}")

        return Edge(
            tactic=tactic,
            src=src_node,
            dst=dst_node,
            time=time,
            logprob=logprob,
        )
    
    async def extend_tree(self, src_node: InternalNode, tactics: List[str]) -> Node:
        """Extend the proof tree by applying a sequence of tactics from a starting node."""
        dst_node = src_node
        for tactic in tactics:
            response, time = await self.run_tactic(dst_node, tactic, cached=True)
            if not isinstance(response, TacticState):
                logger.error(f"Failed to extend tree. Tactic '{tactic}' resulted in {response}.")
                raise RuntimeError(f"Tactic failed during tree extension: {tactic}")
            
            # Create edge and new node
            edge = self.create_edge(
                src_node=dst_node,
                tactic=tactic,
                time=time,
                logprob=1.0,  # Plan/cache tactics have 1.0 logprob
                response=response,
            )
            dst_node.out_edges = [edge]
            dst_node = edge.dst
            dst_node.in_edges.append(edge)
        
        return dst_node


class BestFirstSearch:

    def __init__(
        self,
        tac_gen: TacticGenerator,
        dojo: Dojo,
        root: InternalNode,
        plan: Optional[List[str]],
        timeout: int,
        sampling_params: SamplingParams,
        depth_reward: float = 0.0,
        vllm_timeout: int = 60,
        tactic_timeout: int = 10,
    ) -> None:
        self.tac_gen = tac_gen
        self.sampling_params = sampling_params
        self.dojo = dojo
        self.root = root
        self.plan = plan
        self.progress = 0
        self.nodes: Dict[TacticResult, Node] = {root.state: root}
        self.priority_queue = asyncio.PriorityQueue()
        self.timeout = timeout
        self.depth_reward = depth_reward
        self.vllm_timeout = vllm_timeout
        self.tactic_timeout = tactic_timeout
        self.tactic_manager = TacticManager(
            tac_gen=self.tac_gen,
            dojo=self.dojo,
            has_plan=bool(self.plan),
            sampling_params=self.sampling_params,
            vllm_timeout=self.vllm_timeout,
            tactic_timeout=self.tactic_timeout
        )

        # Profiling info
        self.total_time = 0
        self.explored_nodes = 0

        # Get progress cache
        try:
            self.progress_cache = ray.get_actor("progress_cache")
        except ValueError:
            raise ValueError("Progress cache actor not found!")

    async def run(self) -> None:
        def should_terminate(bfs_start: float) -> Tuple[bool, Optional[str]]:
            """
            Check if the search should be terminated. 
            Return True if the search should be terminated, otherwise return False.
            """
            if self.priority_queue.empty():
                return True, "Ran out of nodes to search."
            
            self.total_time = time.time() - bfs_start
            if self.total_time > self.timeout:
                return True, "Search timed out."
            
            if self.root.status == Status.PROVED:
                return True, "Found a proof!"
            
            if self.root.status == Status.FAILED:
                return True, "Failed early!"
            
            return False, None
        
        def reset_search_root(search_root: InternalNode):
            """Reset the search root for a new search phase"""
            self.priority_queue = asyncio.PriorityQueue()
            search_root.cumulative_logprob, search_root.depth = 0.0, 0
            self.nodes = {search_root.state: search_root}
            self.priority_queue.put_nowait((-search_root.priority, search_root))

        try:
            search_root = self.root
            theorem = self.dojo.entry

            if self.plan:
                await self.progress_cache.initialize.remote(theorem, len(self.plan))

            while self.plan and self.progress < len(self.plan):
                cached_progress = await self.progress_cache.get_progress.remote(theorem)
                if cached_progress > self.progress:
                    logger.info(f"Loading progress from cache while at step {self.progress}, progress={cached_progress}")
                    if cached_progress > len(self.plan):
                        self.root.status = Status.PROVED
                        return
                    plan_solved = []
                    for i in range(self.progress, cached_progress):
                        plan_solved += [self.plan[i], "sorry"]
                    search_root = await self.tactic_manager.extend_tree(search_root, plan_solved)
                
                if cached_progress < len(self.plan):
                    search_root = await self.tactic_manager.extend_tree(search_root, [self.plan[cached_progress]])
                search_root.in_edges[0].src._marker = True
                self.progress = cached_progress

                # Reset the search root
                bfs_start = time.time()
                reset_search_root(search_root)

                while True:
                    should_stop, reason = should_terminate(bfs_start)
                    if should_stop:
                        logger.info(reason)
                        return

                    try:
                        node_with_one_goal = await self._best_first_expansion_step()

                        # If the proof of subgoal finished, update cache and progress
                        if node_with_one_goal:
                            # Update the progress cache if it is available
                            proof_edges = node_with_one_goal.extract_trace()
                            proof, proof_stats, preference_pairs = extract_proof_data(proof_edges, focus_mode=True)
                            await self.progress_cache.update.remote(theorem, self.progress, proof, proof_stats, preference_pairs)

                            # Update the progress
                            self.progress += 1
                            search_root = node_with_one_goal
                            logger.info(f"Found node with 1 goal. Entering next search loop. Progress={self.progress}")
                            break

                    except DojoRestartRequired:
                        raise  # Propagate up to trigger search restart
                    except Exception as ex:
                        logger.info(f"Expansion failed with exception {ex}")

            # Reset the search root
            bfs_start = time.time()
            reset_search_root(search_root)
            
            while True:
                should_stop, reason = should_terminate(bfs_start)
                if should_stop:
                    # If we found a proof in final search, update cache
                    if self.root.status == Status.PROVED and self.plan:
                        proof_edges = search_root.extract_proof()
                        proof, proof_stats, preference_pairs = extract_proof_data(proof_edges, focus_mode=bool(self.plan))
                        await self.progress_cache.update.remote(self.dojo.entry, self.progress, proof, proof_stats, preference_pairs)
                        self.progress += 1
                    logger.info(reason)
                    break

                try:
                    await self._best_first_expansion_step()
                except DojoRestartRequired:
                    raise  # Propagate up to trigger search restart
                except Exception as ex:
                    logger.info(f"Expansion failed with exception {ex}")

        finally:
            if hasattr(self.tactic_manager, "dojo"):
                clean_old_dojo(self.tactic_manager.dojo)

    async def _best_first_expansion_step(self) ->  Optional[InternalNode]:
        # Get the node with highest priority. Return the node for plan if it has only one goal.
        try:
            _, search_node = self.priority_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

        if not isinstance(search_node, InternalNode):
            raise ValueError(
                f"Expected InternalNode upon expansion, got {type(search_node)}"
            )

        # Generate tactic suggestions for the current node
        if self.plan:
            query_state = search_node.state.pp1
        else:
            query_state = search_node.state.pp
        suggestions = await self.tactic_manager.generate_tactics(query_state)
        out_edges = []
        node_for_plan = None
        
        for tactic, logprob in suggestions:

            # Run the tactic and get the response
            response, execution_time = await self.tactic_manager.run_tactic(
                search_node, tactic,
            )

            if response in self.nodes:
                # Reuse an existing node, if a response is repeated.
                edge = self.tactic_manager.create_edge(
                    src_node=search_node,
                    tactic=tactic,
                    time=execution_time,
                    logprob=logprob,
                    dst_node=self.nodes[response],
                )
                if np.random.rand() < LOG_RATE and isinstance(edge.dst, InternalNode):
                    logger.info(f"Circled back to a previous node: {edge.dst}")

            else:
                # Create a new node, if a new response is found.
                edge = self.tactic_manager.create_edge(
                    src_node=search_node,
                    tactic=tactic,
                    time=execution_time,
                    logprob=logprob,
                    response=response,
                )
                if np.random.rand() < LOG_RATE_FREQUENT_EVENT:
                    logger.info(f"Created a new node: {edge.dst}")

                # Add the new node to the priority queue, if it is open
                self.nodes[response] = edge.dst
                if edge.dst.status == Status.OPEN:
                    self.priority_queue.put_nowait(
                        (
                            -edge.dst.priority
                            / ((edge.dst.depth) ** self.depth_reward),
                            edge.dst,
                        )
                    )

            # add the new edge to the out-edges of the src node
            out_edges.append(edge)
            
            # add the new edge to the in-edges of the dst node
            if isinstance(edge.dst, InternalNode):
                edge.dst.in_edges.append(edge)

                # Set the node for plan if the plan is not finished and the node has only one goal
                if self.plan and len(edge.dst.state.goals) == 1 and self.progress < len(self.plan):
                    node_for_plan = edge.dst
                    break
            
            # stop expanding if a proof is found
            if isinstance(response, ProofFinished) or isinstance(response, ProofGivenUp):
                assert isinstance(edge.dst, ProofFinishedNode)
                break

        search_node.out_edges = out_edges
        self.explored_nodes += 1
        self.priority_queue.task_done()
        return node_for_plan

@ray.remote
class Prover:
    """A prover that uses best-first search to find proofs using a tactic generator."""

    MAX_DOJO_RESTARTS = 3

    def __init__(
        self,
        tac_gen: TacticGenerator,
        timeout: int,
        n_sampling_search: int = 16,
        sampling_temperature: float = 0.7,
        sampling_top_p: float = 1.0,
        max_tokens: int = 2048,
        depth_reward: float = 0.0,
        vllm_timeout: int = 60,
        tactic_timeout: int = 10,
    ) -> None:
        self.tac_gen = tac_gen
        self.timeout = timeout
        self.n_sampling_search = n_sampling_search
        self.sampling_temperature = sampling_temperature
        self.sampling_top_p = sampling_top_p
        self.max_tokens = max_tokens
        self.depth_reward = depth_reward
        self.vllm_timeout = vllm_timeout
        self.tactic_timeout = tactic_timeout

    # Search for a proof with a single attempt
    async def _search(
        self, task: ProofTask, attempt_number: int = 0
    ) -> Optional[SearchResult]:
        logger.info(f"Proving {task.theorem}, attempt {attempt_number}")
        try:
            with Dojo(task.theorem, self.timeout) as (
                dojo,
                init_state,
            ):
                # init_state = dojo.run_tac(init_state, "suffices : True")
                plan = None
                if task.plan:
                    plan = task.plan
                    logger.info(f"Using plan for theorem {task.theorem.full_name}")
                else:
                    logger.info("No plans available, proceeding without plan")
                root = InternalNode(
                    state=init_state,
                    cumulative_logprob=0.0,
                    depth=0,
                )
                sampling_params = SamplingParams(
                    n=self.n_sampling_search,
                    temperature=self.sampling_temperature,
                    top_p=self.sampling_top_p,
                    logprobs=1,
                    max_tokens=self.max_tokens,
                )
                bfs = BestFirstSearch(
                    tac_gen=self.tac_gen,
                    dojo=dojo,
                    root=root,
                    plan=plan,
                    timeout=self.timeout,
                    sampling_params=sampling_params,
                    depth_reward=self.depth_reward,
                    vllm_timeout=self.vllm_timeout,
                    tactic_timeout=self.tactic_timeout,
                )
                try:
                    await bfs.run()
                except DojoRestartRequired as ex:
                    # Dojo was restarted due to hard timeout, retry the search
                    if attempt_number < self.MAX_DOJO_RESTARTS:
                        logger.warning(
                            f"Dojo restart triggered for {task.theorem.full_name}, "
                            f"retrying search (attempt {attempt_number + 1}/{self.MAX_DOJO_RESTARTS}): {ex}"
                        )
                        return await self._search(task, attempt_number + 1)
                    else:
                        logger.error(
                            f"Max dojo restarts ({self.MAX_DOJO_RESTARTS}) reached for {task.theorem.full_name}, giving up"
                        )
                        return None
                except Exception as ex:
                    logger.warning(
                        f"Search failed for Theorem {task.theorem}" f" with exception {ex}"
                    )
                    return None

            # Validate the proof
            proof, proof_stats, preference_pairs, proof_valid = None, None, None, None
            if root.status == Status.PROVED:
                if plan: # with planner, get proof stats from progress cache
                        proof, proof_stats, preference_pairs = await bfs.progress_cache.get_proof_data.remote(task.theorem)
                        if proof:
                            proof_trajectory = [tactic for _, tactic in proof]
                            proof_valid = validate_proof(task.theorem, proof_trajectory, ProofFinished, focus_mode=True)
                            if not proof_valid:
                                await bfs.progress_cache.initialize.remote(task.theorem, len(plan), replace=True)
                else: # without planner, extract proof stats from the proof tree
                    proof_edges = root.extract_proof()
                    proof, proof_stats, preference_pairs = extract_proof_data(proof_edges)
                    if proof:
                        proof_trajectory = [tactic for _, tactic in proof]
                        proof_valid = validate_proof(task.theorem, proof_trajectory, ProofFinished)

            search_result = SearchResult(
                url=task.theorem.repo.url,
                commit=task.theorem.repo.commit,
                full_name=task.theorem.full_name,
                file_path=str(task.theorem.file_path),
                status=root.status,
                proof=proof,
                proof_stats=proof_stats,
                preference_pairs=preference_pairs,
                tactic_time=bfs.tactic_manager.tactic_time,
                vllm_time=bfs.tactic_manager.vllm_time,
                total_time=bfs.total_time,
                total_nodes=len(bfs.nodes),
                explored_nodes=bfs.explored_nodes,
                total_attempts=attempt_number + 1,
                proof_validation_passed=proof_valid,
            )
            logger.info(search_result)
            return search_result

        except Exception as ex:
            logger.warning(
                f"Dojo initialization failed for Theorem {task.theorem} with exception {ex}"
            )
            return None

    # Search for a proof with single attempt
    async def search_attempt(
        self, task: ProofTask, attempt_number: int = 0
    ) -> Optional[SearchResult]:
        return await self._search(task, attempt_number)

class ProverManager:
    TOTAL_GPUS = torch.cuda.device_count()

    def __init__(
        self,
        model_path: str,
        max_num_batched_tokens: int,
        max_num_seqs: int,
        num_tac_gen: int,
        num_provers: int,
        num_gpus_per_tac_gen: int = 1,
        num_cpus_per_prover: int = 1,
        timeout_per_theorem: int = 600,
        n_sampling_search: int = 16,
        sampling_temperature: float = 0.7,
        sampling_top_p: float = 1.0,
        max_tokens: int = 2048,
        depth_reward: float = 0.0,
        vllm_timeout: int = 60,
        tactic_timeout: int = 10,
    ) -> None:
        assert num_tac_gen * num_gpus_per_tac_gen <= self.TOTAL_GPUS

        logger.info(
            f"Initializing ProverManager with {num_tac_gen} tactic generators and {num_provers} provers..."
        )
        # Create tactic generators
        logger.info("Creating tactic generators...")

        self.tactic_generators = [
            TacticGenerator.options(num_gpus=num_gpus_per_tac_gen).remote(
                model_path=model_path,
                max_num_batched_tokens=max_num_batched_tokens,
                max_num_seqs=max_num_seqs,
            )
            for _ in range(num_tac_gen)
        ]

        # Wait for all tactic generators to be ready
        logger.info("Waiting for tactic generators to be ready...")
        self._wait_for_models_ready(timeout=MODEL_READINESS_TIMEOUT)

        self.provers = [
            Prover.options(num_cpus=num_cpus_per_prover).remote(
                tac_gen=self.tactic_generators[i % num_tac_gen],
                timeout=timeout_per_theorem,
                n_sampling_search=n_sampling_search,
                sampling_temperature=sampling_temperature,
                sampling_top_p=sampling_top_p,
                max_tokens=max_tokens,
                depth_reward=depth_reward,
                vllm_timeout=vllm_timeout,
                tactic_timeout=tactic_timeout,
            )
            for i in range(num_provers)
        ]
        self.prover_pool = ActorPool(self.provers)

        logger.info(
            f"Initialized {num_tac_gen} tactic generators, each with {num_gpus_per_tac_gen} GPU(s), "
            f"and {num_provers} provers, each with {num_cpus_per_prover} CPU(s)"
        )

    def _wait_for_models_ready(self, timeout: int = 300) -> None:
        """Wait for all tactic generators to be ready before proceeding."""
        logger.info(
            f"Waiting for all {len(self.tactic_generators)} tactic generators to be ready..."
        )
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # Check if all generators are ready
                ready_futures = [
                    tac_gen.is_ready.remote() for tac_gen in self.tactic_generators
                ]
                ready_results = ray.get(ready_futures, timeout=10)
                if all(ready_results):
                    elapsed_time = time.time() - start_time
                    logger.info(
                        f"All {len(self.tactic_generators)} tactic generators are ready! (took {elapsed_time:.2f}s)"
                    )
                    return
                else:
                    ready_count = sum(ready_results)
                    elapsed_time = time.time() - start_time
                    logger.info(
                        f"{ready_count}/{len(self.tactic_generators)} tactic generators ready, waiting... (elapsed: {elapsed_time:.2f}s)"
                    )
                    time.sleep(10)
            except Exception as e:
                elapsed_time = time.time() - start_time
                logger.warning(
                    f"Tactic generators not ready yet, elapsed: {elapsed_time:.2f}s, log: {e}"
                )
                time.sleep(10)

        logger.warning(
            f"Not all tactic generators become ready within {timeout} seconds"
        )
        return

    def parallel_prove_pass_k(
        self, tasks: List[ProofTask], k: int = 1
    ) -> List[Optional[SearchResult]]:
        # Submit all attempts and track futures per task
        task_futures = [[] for _ in tasks]
        future_to_info = {}  # future -> (task_idx, attempt)
        
        # Build submission queue (interleave attempts across tasks for better parallelism)
        pending_submissions = []
        for attempt in range(k):
            for task_idx, task in enumerate(tasks):
                pending_submissions.append((task_idx, task, attempt))
        
        num_provers = len(self.provers)
        futures = set()
        tasks_done = set()
        prover_idx = 0
        
        def submit_next():
            """Helper to submit pending tasks until reaching the number of provers."""
            nonlocal prover_idx
            while pending_submissions and len(futures) < num_provers:
                next_task_idx, next_task, next_attempt = pending_submissions.pop(0)
                if next_task_idx not in tasks_done:
                    next_future = self.provers[prover_idx].search_attempt.remote(next_task, next_attempt)
                    task_futures[next_task_idx].append(next_future)
                    future_to_info[next_future] = (next_task_idx, next_attempt)
                    futures.add(next_future)
                    prover_idx = (prover_idx + 1) % num_provers
        
        # Submit initial batch
        submit_next()
        
        # Process results with early stopping
        results: List[Optional[SearchResult]] = [None] * len(tasks)
        total = 0
        num_proved = 0
        num_verified = 0
        num_crashed = 0
        acc = 0
        with tqdm(total=len(tasks)) as pbar:
            while futures or pending_submissions:
                # Submit more tasks to maintain throughput
                submit_next()
                
                # If no futures, break to avoid empty ray.wait
                if not futures:
                    break
                
                # Wait for at least one future to complete
                ready, _ = ray.wait(list(futures), num_returns=1, timeout=None)
                
                for future in ready:
                    futures.discard(future)
                    task_idx, _ = future_to_info[future]
                    
                    # Skip if task already done
                    if task_idx in tasks_done:
                        continue
                    
                    # Get result
                    result = ray.get(future)
                    
                    # Update best result for this task (prefer verified > proved > others)
                    current = results[task_idx]
                    if not current:
                        results[task_idx] = result
                    elif result and result.status == Status.PROVED:
                        if current.status != Status.PROVED or result.proof_validation_passed:
                            results[task_idx] = result
                    
                    # Check if task should be marked as done
                    task_finish = False
                    
                    # Early stop if the result is verified
                    if result and result.proof_validation_passed:
                        # Cancel remaining futures for this task
                        for other_future in task_futures[task_idx]:
                            if other_future in futures:
                                ray.cancel(other_future, force=False)
                                futures.discard(other_future)
                        # Remove pending submissions for this task
                        pending_submissions = [(idx, t, att) for idx, t, att in pending_submissions if idx != task_idx]
                        task_finish = True
                    # Or if all attempts are done
                    elif (not any(idx == task_idx for idx, _, _ in pending_submissions) 
                            and all(f not in futures for f in task_futures[task_idx])):
                        task_finish = True
                    
                    # Mark task as done and update stats
                    if task_finish:
                        tasks_done.add(task_idx)
                        result = results[task_idx]
                        total += 1
                        if hasattr(result, "status"):
                            if result.status == Status.PROVED:
                                num_proved += 1
                                if result.proof_validation_passed:
                                    num_verified += 1
                        else:
                            num_crashed += 1
                        acc = num_verified / total if total > 0 else 0
                        pbar.update(1)
                        logger.info(
                            f"Worked on {total} theorems so far, {num_proved} proved, {num_verified} verified, {num_crashed} crashed, acc: {acc:.2f}"
                        )
        return results
