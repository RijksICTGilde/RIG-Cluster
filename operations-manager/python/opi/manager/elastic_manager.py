from typing import Any


class ElasticManager:

    def __init__(self, project_manager: "ProjectManager") -> None:
        self.project_manager = project_manager


    async def create_resources_for_deployment(
            self,
            project_data: dict[str, Any],
            deployment: dict[str, Any],
    ) -> None:
        pass


