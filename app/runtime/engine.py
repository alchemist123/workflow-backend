"""Workflow execution engine — traverses the IR graph and executes nodes."""
import asyncio
from datetime import datetime
from typing import Any
from app.compiler.ir import IR, IRNode
from app.runtime.context import ExecutionContext
from app.nodes.registry import get_node_definition
from app.config import get_settings

settings = get_settings()


class WorkflowEngine:
    def __init__(self, db_session: Any = None):
        self.db = db_session

    async def execute(
        self,
        ir: IR,
        execution_id: str,
        trigger_payload: dict[str, Any],
    ) -> dict[str, Any]:
        ctx = ExecutionContext(
            execution_id=execution_id,
            workflow_id="",
            version_id=ir.workflow_version_id,
            trigger_payload=trigger_payload,
            db_session=self.db,
        )

        await self._update_execution_status(execution_id, "running")

        try:
            results = {}
            for entrypoint in ir.entrypoints:
                result = await self._execute_node(ir, entrypoint, trigger_payload, ctx)
                results[entrypoint] = result

            await self._update_execution_status(execution_id, "success", output=results)
            return {"status": "success", "output": results}
        except Exception as e:
            await self._update_execution_status(execution_id, "failed", error=str(e))
            return {"status": "failed", "error": str(e)}

    async def _execute_node(
        self,
        ir: IR,
        node_id: str,
        input_data: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        if node_id not in ir.nodes:
            raise ValueError(f"Node '{node_id}' not found in IR")

        ir_node = ir.nodes[node_id]
        defn = get_node_definition(ir_node.node_type)
        if not defn:
            raise ValueError(f"No definition for node type '{ir_node.node_type}'")

        timeout = ir_node.policies.get("timeout_seconds", settings.default_execution_timeout)
        max_retries = ir_node.policies.get("retry_max_attempts", 1)

        await self._log_node_start(ctx.execution_id, node_id, ir_node.node_type, input_data)

        output: dict[str, Any] = {}
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                output = await asyncio.wait_for(
                    defn.execute(ir_node.config, input_data, ctx),
                    timeout=timeout,
                )
                last_error = None
                break
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"Node '{node_id}' timed out after {timeout}s")
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        if last_error:
            on_error = ir_node.policies.get("on_error", "fail")
            await self._log_node_end(ctx.execution_id, node_id, "failed", input_data, {}, str(last_error))
            if on_error == "fail":
                raise last_error
            output = {"error": str(last_error), "_error": True}

        # Handle suspend (e.g. HUMAN_APPROVAL)
        if output.get("_suspend"):
            await self._log_node_end(ctx.execution_id, node_id, "waiting", input_data, output, None)
            await self._update_execution_status(ctx.execution_id, "waiting")
            return output

        await self._log_node_end(ctx.execution_id, node_id, "success", input_data, output, None)
        ctx.node_outputs[node_id] = output

        # Handle LOOP
        if ir_node.node_type == "LOOP":
            return await self._execute_loop(ir, ir_node, output, ctx)

        # Handle CONDITION branching
        if ir_node.node_type == "CONDITION":
            branch = output.get("_branch", "default")
            next_nodes = ir_node.branches.get(branch, ir_node.branches.get("default", []))
            for next_id in next_nodes:
                output = await self._execute_node(ir, next_id, output, ctx)
            return output

        # Handle PARALLEL_FORK
        if ir_node.node_type == "PARALLEL_FORK":
            branches = output.get("_parallel_branches", [])
            tasks = []
            for branch_name in branches:
                next_ids = ir_node.branches.get(branch_name, [])
                for next_id in next_ids:
                    tasks.append(self._execute_node(ir, next_id, output, ctx))
            branch_results = await asyncio.gather(*tasks, return_exceptions=True)
            return {"branch_results": branch_results}

        # Default: follow next edges
        for next_id in ir_node.next:
            output = await self._execute_node(ir, next_id, output, ctx)

        return output

    async def _execute_loop(
        self,
        ir: IR,
        ir_node: IRNode,
        state: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        config = ir_node.config
        mode = config.get("mode", "for_each")
        max_iterations = min(config.get("max_iterations", 100), settings.max_loop_iterations)
        results = []
        loop_body_next = ir_node.branches.get("loop_body", ir_node.next[:])
        done_next = ir_node.branches.get("done", [])

        if mode == "for_each":
            items_path = config.get("items_path", "")
            items = state.get("data", {})
            for part in items_path.split("."):
                if isinstance(items, dict):
                    items = items.get(part, [])
            if not isinstance(items, list):
                items = []

            for idx, item in enumerate(items[:max_iterations]):
                item_data = {"current_item": item, "current_index": idx, **state}
                iteration_output = item_data
                for next_id in loop_body_next:
                    iteration_output = await self._execute_node(ir, next_id, item_data, ctx)
                results.append(iteration_output)

        elif mode == "while":
            exit_expr = config.get("exit_condition", "True")
            current_data = state
            safe_globals: dict = {"__builtins__": {}}
            for i in range(max_iterations):
                safe_locals = {"data": current_data, "i": i}
                try:
                    should_exit = eval(exit_expr, safe_globals, safe_locals)  # noqa: S307
                except Exception:
                    should_exit = True
                if should_exit:
                    break
                iteration_output = current_data
                for next_id in loop_body_next:
                    iteration_output = await self._execute_node(ir, next_id, current_data, ctx)
                results.append(iteration_output)
                current_data = iteration_output

        final_output = {"results": results, "iterations": len(results)}

        for next_id in done_next:
            final_output = await self._execute_node(ir, next_id, final_output, ctx)

        return final_output

    async def _update_execution_status(self, execution_id: str, status: str, output: dict | None = None, error: str | None = None) -> None:
        if not self.db:
            return
        from app.models.workflow import WorkflowExecution, ExecutionStatus
        from sqlalchemy import select
        result = await self.db.execute(select(WorkflowExecution).where(WorkflowExecution.id == execution_id))
        execution = result.scalar_one_or_none()
        if execution:
            execution.status = ExecutionStatus(status)
            if status in ("success", "failed"):
                execution.finished_at = datetime.utcnow()
            if status == "running":
                execution.started_at = datetime.utcnow()
            if output:
                execution.output = output
            if error:
                execution.error = error
            await self.db.commit()

    async def _log_node_start(self, execution_id: str, node_id: str, node_type: str, input_data: dict) -> None:
        if not self.db:
            return
        from app.models.workflow import NodeExecutionLog, ExecutionStatus
        log = NodeExecutionLog(
            execution_id=execution_id,
            node_id=node_id,
            node_type=node_type,
            status=ExecutionStatus.RUNNING,
            input_data=input_data,
            started_at=datetime.utcnow(),
        )
        self.db.add(log)
        await self.db.flush()

    async def _log_node_end(self, execution_id: str, node_id: str, status: str, input_data: dict, output: dict, error: str | None) -> None:
        if not self.db:
            return
        from app.models.workflow import NodeExecutionLog, ExecutionStatus
        from sqlalchemy import select
        result = await self.db.execute(
            select(NodeExecutionLog)
            .where(NodeExecutionLog.execution_id == execution_id, NodeExecutionLog.node_id == node_id)
            .order_by(NodeExecutionLog.started_at.desc())
        )
        log = result.scalars().first()
        if log:
            log.status = ExecutionStatus(status)
            log.output_data = output
            log.error = error
            log.finished_at = datetime.utcnow()
            await self.db.flush()
