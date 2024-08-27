from importlib import util
from importlib.abc import SourceLoader


def get_module_code(module_name):
    spec = util.find_spec(module_name)
    if spec and isinstance(spec.loader, SourceLoader):
        source = spec.loader.get_source(module_name)
        if source:
            return source
    raise FileNotFoundError(f"Source code for module {module_name} not found.")


class Description:

    @staticmethod
    def of_module(module_name, intro_text):
        try:
            module_code = get_module_code(module_name)
            module_description = (
                intro_text +
                "```" + module_code + "```"
            )
            return module_description
        except FileNotFoundError as e:
            print(e)
            return None

    def of_petritype_data_structures():
        return Description.of_module(
            "petritype.core.data_structures",
            "BACKGROUND: The following data structures are used:\n"
        )

    def of_petritype_relationship_graph_components():
        return Description.of_module(
            "petritype.core.relationship_graph_components",
            "INTRO: The following code describes the components of a Petritype Relationship Graph in our context.\n"
        )
