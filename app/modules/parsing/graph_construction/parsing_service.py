import logging
import os
import shutil
import traceback
from contextlib import contextmanager

from blar_graph.db_managers import Neo4jManager
from blar_graph.graph_construction.core.graph_builder import GraphConstructor
from fastapi import HTTPException
from git import Repo
from sqlalchemy.orm import Session

from app.core.config_provider import config_provider
from app.modules.github.github_service import GithubService
from app.modules.parsing.graph_construction.code_graph_service import CodeGraphService
from app.modules.parsing.graph_construction.parsing_helper import (
    ParseHelper,
    ParsingFailedError,
    ParsingServiceError,
)
from app.modules.parsing.knowledge_graph.inference_service import InferenceService
from app.modules.projects.projects_schema import ProjectStatusEnum
from app.modules.projects.projects_service import ProjectService
from app.modules.search.search_service import SearchService
from app.modules.utils.posthog_helper import PostHogClient

from .parsing_schema import ParsingRequest

logger = logging.getLogger(__name__)


class ParsingService:
    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.parse_helper = ParseHelper(db)
        self.project_service = ProjectService(db)
        self.inference_service = InferenceService(db, user_id)
        self.search_service = SearchService(db)
        self.github_service = GithubService(db)

    @contextmanager
    def change_dir(self, path):
        old_dir = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old_dir)

    async def parse_directory(
        self,
        repo_details: ParsingRequest,
        user_id: str,
        user_email: str,
        project_id: int,
        cleanup_graph: bool = True,
    ):
        project_manager = ProjectService(self.db)
        extracted_dir = None

        try:
            if cleanup_graph:
                neo4j_config = config_provider.get_neo4j_config()

                try:
                    code_graph_service = CodeGraphService(
                        neo4j_config["uri"],
                        neo4j_config["username"],
                        neo4j_config["password"],
                        self.db,
                    )

                    await code_graph_service.cleanup_graph(project_id)
                except Exception as e:
                    logger.error(f"Error in cleanup_graph: {e}")
                    raise HTTPException(status_code=500, detail="Internal server error")
            # Remove self.db from the arguments
            repo, owner, auth = await self.parse_helper.clone_or_copy_repository(
                repo_details, user_id
            )
            extracted_dir, project_id = await self.parse_helper.setup_project_directory(
                repo, repo_details.branch_name, auth, repo, user_id, project_id
            )

            if isinstance(repo, Repo):
                language = self.parse_helper.detect_repo_language(extracted_dir)
            else:
                languages = repo.get_languages()
                language = max(languages, key=languages.get).lower()

            await self.analyze_directory(
                extracted_dir, project_id, user_id, self.db, language
            )
            message = "The project has been parsed successfully"
            return {"message": message, "id": project_id}

        except ParsingServiceError as e:
            message = str(f"{project_id} Failed during parsing: " + str(e))
            await project_manager.update_project_status(
                project_id, ProjectStatusEnum.ERROR
            )
            raise HTTPException(status_code=500, detail=message)

        except Exception as e:
            await project_manager.update_project_status(
                project_id, ProjectStatusEnum.ERROR
            )
            tb_str = "".join(traceback.format_exception(None, e, e.__traceback__))
            raise HTTPException(
                status_code=500, detail=f"{str(e)}\nTraceback: {tb_str}"
            )

        finally:
            if (
                extracted_dir
                and os.path.exists(extracted_dir)
                and extracted_dir.startswith(os.getenv("PROJECT_PATH"))
            ):
                shutil.rmtree(extracted_dir, ignore_errors=True)


    def create_neo4j_indices(self, graph_manager):
            graph_manager.create_entityId_index()
            graph_manager.create_node_id_index()
            # graph_manager.create_function_name_index()
            # with graph_manager.driver.session() as session:
            #     node_query = """
            #     CREATE INDEX repo_id_node_id_NODE IF NOT EXISTS FOR (n:NODE) ON (n.repoId, n.node_id)
            #     """
            #     session.run(node_query)

    async def analyze_directory(
        self, extracted_dir: str, project_id: int, user_id: str, db, language: str
    ):
        logger.info(
            f"Parsing project {project_id}: Analyzing directory: {extracted_dir}"
        )

        if language in ["python", "javascript", "typescript"]:
            graph_manager = Neo4jManager(project_id, user_id)
            self.create_neo4j_indices(graph_manager)

            try:
                graph_constructor = GraphConstructor(graph_manager, user_id)
                n, r = graph_constructor.build_graph(extracted_dir)
                if len(n) == 0:
                    n, r = graph_constructor.build_graph(extracted_dir)
                    if len(n) == 0:
                        logger.error(f"Project: {project_id} Failed to build graph")
                        raise ParsingFailedError(f"Project: {project_id} Failed to build graph")
                graph_manager.create_nodes(n)
                graph_manager.create_edges(r)

                await self.project_service.update_project_status(
                    project_id, ProjectStatusEnum.PARSED
                )
                PostHogClient().send_event(
                    user_id,
                    "project_status_event",
                    {"project_id": project_id, "status": "Parsed"},
                )

                # Generate docstrings using InferenceService
                await self.inference_service.run_inference(project_id)
                logger.info(f"DEBUGNEO4J: After inference project {project_id}")
                self.inference_service.log_graph_stats(project_id)
                await self.project_service.update_project_status(
                    project_id, ProjectStatusEnum.READY
                )
                PostHogClient().send_event(
                    user_id,
                    "project_status_event",
                    {"project_id": project_id, "status": "Ready"},
                )
            except Exception as e:
                logger.error(e)
                logger.error(traceback.format_exc())
                await self.project_service.update_project_status(
                    project_id, ProjectStatusEnum.ERROR
                )
                PostHogClient().send_event(
                    user_id,
                    "project_status_event",
                    {"project_id": project_id, "status": "Error"},
                )
            finally:
                graph_manager.close()
        elif language != "other":
            try:
                neo4j_config = config_provider.get_neo4j_config()
                service = CodeGraphService(
                    neo4j_config["uri"],
                    neo4j_config["username"],
                    neo4j_config["password"],
                    db,
                )

                service.create_and_store_graph(extracted_dir, project_id, user_id)

                await self.project_service.update_project_status(
                    project_id, ProjectStatusEnum.PARSED
                )
                # Generate docstrings using InferenceService
                await self.inference_service.run_inference(project_id)
                logger.info(f"DEBUGNEO4J: After inference project {project_id}")
                self.inference_service.log_graph_stats(project_id)
                await self.project_service.update_project_status(
                    project_id, ProjectStatusEnum.READY
                )
                logger.info(f"DEBUGNEO4J: After update project status {project_id}")
                self.inference_service.log_graph_stats(project_id)
            finally:
                service.close()
                logger.info(f"DEBUGNEO4J: After close service {project_id}")
                self.inference_service.log_graph_stats(project_id)
        else:
            await self.project_service.update_project_status(
                project_id, ProjectStatusEnum.ERROR
            )
            logger.info(f"DEBUGNEO4J: After update project status {project_id}")
            self.inference_service.log_graph_stats(project_id)
            raise ParsingFailedError(
                "Repository doesn't consist of a language currently supported."
            )

