import logging
import sys
from pathlib import Path
from typing import Any, List

import orjson
from hamilton import base
from hamilton.experimental.h_async import AsyncDriver
from haystack import component
from haystack.components.builders.prompt_builder import PromptBuilder
from langfuse.decorators import observe

from src.core.pipeline import BasicPipeline
from src.core.provider import LLMProvider
from src.utils import async_timer, timer

logger = logging.getLogger("wren-ai-service")


sql_summary_system_prompt = """
### TASK ###
You are a great data analyst. You are now given a task to summarize a list SQL queries in a human-readable format where each summary should be within 10-20 words.
You will be given a list of SQL queries and a user's question.


### OUTPUT FORMAT ###
Please return the result in the following JSON format:

{
    "sql_summary_results": [
        {"summary": <SQL_QUERY_SUMMARY_1>},
        {"summary": <SQL_QUERY_SUMMARY_2>},
        {"summary": <SQL_QUERY_SUMMARY_3>}
    ]
}
"""

sql_summary_user_prompt_template = """
User's Question: {{query}}
SQLs: {{sqls}}

Please think step by step.
"""


@component
class SQLSummaryPostProcessor:
    @component.output_types(
        sql_summary_results=List[str],
    )
    def run(self, sqls: List[str], replies: List[str]):
        try:
            return {
                "sql_summary_results": [
                    {"sql": sql["sql"], "summary": summary["summary"]}
                    for (sql, summary) in zip(
                        sqls, orjson.loads(replies[0])["sql_summary_results"]
                    )
                ],
            }
        except Exception as e:
            logger.exception(f"Error in SQLSummaryPostProcessor: {e}")

            return {
                "sql_summary_results": [],
            }


## Start of Pipeline
@timer
@observe(capture_input=False)
def prompt(
    query: str,
    sqls: List[str],
    prompt_builder: PromptBuilder,
) -> dict:
    logger.debug(f"query: {query}")
    logger.debug(f"sqls: {sqls}")

    return prompt_builder.run(
        query=query,
        sqls=sqls,
    )


@async_timer
@observe(as_type="generation", capture_input=False)
async def generate(prompt: dict, generator: Any) -> dict:
    logger.debug(f"prompt: {orjson.dumps(prompt, option=orjson.OPT_INDENT_2).decode()}")
    return await generator.run(prompt=prompt.get("prompt"))


@timer
def post_process(
    generate: dict,
    sqls: List[str],
    post_processor: SQLSummaryPostProcessor,
) -> dict:
    logger.debug(
        f"generate: {orjson.dumps(generate, option=orjson.OPT_INDENT_2).decode()}"
    )
    return post_processor.run(sqls, generate.get("replies"))


## End of Pipeline


class SQLSummary(BasicPipeline):
    def __init__(
        self,
        llm_provider: LLMProvider,
    ):
        self.generator = llm_provider.get_generator(
            system_prompt=sql_summary_system_prompt
        )
        self.prompt_builder = PromptBuilder(template=sql_summary_user_prompt_template)
        self.post_processor = SQLSummaryPostProcessor()

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    def visualize(
        self,
        query: str,
        sqls: List[str],
    ) -> None:
        destination = "outputs/pipelines/ask"
        if not Path(destination).exists():
            Path(destination).mkdir(parents=True, exist_ok=True)

        self._pipe.visualize_execution(
            ["post_process"],
            output_file_path=f"{destination}/sql_summary.dot",
            inputs={
                "query": query,
                "sqls": sqls,
                "generator": self.generator,
                "prompt_builder": self.prompt_builder,
                "post_processor": self.post_processor,
            },
            show_legend=True,
            orient="LR",
        )

    @async_timer
    @observe(name="SQL Summary")
    async def run(
        self,
        query: str,
        sqls: List[str],
    ):
        logger.info("SQL Summary pipeline is running...")
        return await self._pipe.execute(
            ["post_process"],
            inputs={
                "query": query,
                "sqls": sqls,
                "generator": self.generator,
                "prompt_builder": self.prompt_builder,
                "post_processor": self.post_processor,
            },
        )


if __name__ == "__main__":
    from langfuse.decorators import langfuse_context

    from src.core.engine import EngineConfig
    from src.core.pipeline import async_validate
    from src.utils import init_langfuse, init_providers, load_env_vars

    load_env_vars()
    init_langfuse()

    llm_provider, _, _, _ = init_providers(engine_config=EngineConfig())
    pipeline = SQLSummary(
        llm_provider=llm_provider,
    )

    pipeline.visualize("", [])
    async_validate(lambda: pipeline.run("", []))

    langfuse_context.flush()
