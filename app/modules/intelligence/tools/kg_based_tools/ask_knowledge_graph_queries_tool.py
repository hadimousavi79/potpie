import asyncio
import logging
import os
from typing import Any, Dict, List

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

from app.modules.parsing.knowledge_graph.inference_schema import QueryResponse
from app.modules.parsing.knowledge_graph.inference_service import InferenceService
from app.modules.projects.projects_service import ProjectService
from app.modules.intelligence.tools.tool_schema import ToolParameter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):
    node_ids: List[str] = Field(description="A list of node ids to query")
    project_id: str = Field(
        description="The project id metadata for the project being evaluated"
    )
    query: str = Field(
        description="A natural language question to ask the knowledge graph"
    )


class MultipleKnowledgeGraphQueriesInput(BaseModel):
    queries: List[str] = Field(
        description="A list of natural language questions to ask the knowledge graph"
    )
    project_id: str = Field(
        description="The project id metadata for the project being evaluated"
    )


class KnowledgeGraphQueryTool:
    name = "ask_knowledge_graph_queries"
    description = (
        "Query the code knowledge graph using multiple natural language questions"
    )

    def __init__(self, sql_db, user_id):
        self.kg_query_url = os.getenv("KNOWLEDGE_GRAPH_URL")
        self.headers = {"Content-Type": "application/json"}
        self.user_id = user_id
        self.sql_db = sql_db

    async def ask_multiple_knowledge_graph_queries(
        self, queries: List[QueryRequest]
    ) -> Dict[str, str]:
        inference_service = InferenceService(self.sql_db, "dummy")

        async def process_query(query_request: QueryRequest) -> List[QueryResponse]:
            # Call the query_vector_index method directly from InferenceService
            results = inference_service.query_vector_index(
                query_request.project_id, query_request.query, query_request.node_ids
            )
            return [
                QueryResponse(
                    node_id=result.get("node_id"),
                    docstring=result.get("docstring"),
                    file_path=result.get("file_path"),
                    start_line=result.get("start_line") or 0,
                    end_line=result.get("end_line") or 0,
                    similarity=result.get("similarity") or 0,
                )
                for result in results
            ]

        tasks = [process_query(query) for query in queries]
        results = await asyncio.gather(*tasks)

        return results

    def run_tool(
        self, queries: List[str], project_id: str, node_ids: List[str] = []
    ) -> Dict[str, str]:
        # Create a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Run the coroutine using the event loop
        return loop.run_until_complete(
            self.ask_knowledge_graph_query(queries, project_id, node_ids)
        )

    async def ask_knowledge_graph_query(
        self, queries: List[str], project_id: str, node_ids: List[str] = []
    ) -> Dict[str, str]:
        """
        Query the code knowledge graph using multiple natural language questions.
        The knowledge graph contains information about every function, class, and file in the codebase.
        This method allows asking multiple questions about the codebase in a single operation.

        Inputs:
        - queries (List[str]): A list of natural language questions that the user wants to ask the knowledge graph.
          Each question should be clear and concise, related to the codebase.
        - project_id (str): The ID of the project being evaluated, this is a UUID.
        - node_ids (List[str]): A list of node ids to query, this is an optional parameter that can be used to query a specific node.

        Returns:
        - Dict[str, str]: A dictionary where keys are the original queries and values are the corresponding responses.
        """
        project = await ProjectService(self.sql_db).get_project_repo_details_from_db(
            project_id, self.user_id
        )

        if not project:
            raise ValueError(
                f"Project with ID '{project_id}' not found in database for user '{self.user_id}'"
            )
        project_id = project["id"]
        query_list = [
            QueryRequest(query=query, project_id=project_id, node_ids=node_ids)
            for query in queries
        ]
        return await self.ask_multiple_knowledge_graph_queries(query_list)

    async def run(
        self, queries: List[str], repo_id: str, node_ids: List[str] = []
    ) -> Dict[str, Any]:
        try:
            results = await self.ask_knowledge_graph_query(queries, repo_id, node_ids)
            return results
        except Exception as e:
            logger.error(f"Unexpected error in KnowledgeGraphQueryTool: {str(e)}")
            return {"error": f"An unexpected error occurred: {str(e)}"}

    @staticmethod
    def get_parameters() -> List[ToolParameter]:
        return [
            ToolParameter(
                name="repo_id",
                type="string",
                description="The repository ID (UUID)",
                required=True
            ),
            ToolParameter(
                name="query",
                type="string",
                description="The knowledge graph query to execute",
                required=True
            )
        ]


def get_ask_knowledge_graph_queries_tool(sql_db, user_id) -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=KnowledgeGraphQueryTool(sql_db, user_id).run,
        func=KnowledgeGraphQueryTool(sql_db, user_id).run_tool,
        name="Ask Knowledge Graph Queries",
        description="""
    Query the code knowledge graph using multiple natural language questions.
    The knowledge graph contains information about every function, class, and file in the codebase.
    This tool allows asking multiple questions about the codebase in a single operation.

    Inputs:
    - queries (List[str]): A list of natural language questions to ask the knowledge graph. Each question should be
    clear and concise, related to the codebase, such as "What does the XYZ class do?" or "How is the ABC function used?"
    - project_id (str): The ID of the project being evaluated, this is a UUID.
    - node_ids (List[str]): A list of node ids to query, this is an optional parameter that can be used to query a specific node. use this only when you are sure that the answer to the question is related to that node.

    Use this tool when you need to ask multiple related questions about the codebase at once.
    Do not use this to query code directly.""",
        args_schema=MultipleKnowledgeGraphQueriesInput,
    )
