#!/usr/bin/env python3
"""Convert a SciRalph problem JSON file to individual YAML problem files."""

import json
import os
import sys


def json_to_yamls(json_path: str) -> None:
    with open(json_path) as f:
        data = json.load(f)

    dataset_name = data["dataset_name"]
    output_dir = os.path.dirname(json_path) or "."

    for problem in data["problems"]:
        problem_type = problem["problem_type"]
        problem_index = problem["problem_index"]

        # Build filename
        if problem_type == "main":
            filename = f"{dataset_name}_main.yaml"
        else:
            filename = f"{dataset_name}_sub{problem_index}.yaml"

        # Get problem description, strip leading/trailing whitespace
        description = problem["problem_description"].strip()

        # Indent each line by 2 spaces for YAML literal block scalar
        indented_lines = []
        for line in description.split("\n"):
            if line.strip():
                indented_lines.append(f"  {line}")
            else:
                indented_lines.append("")
        indented = "\n".join(indented_lines)

        # Get answer code
        answer = problem["answer_only_code"].strip()

        # Indent answer lines for YAML literal block scalar
        answer_lines = []
        for line in answer.split("\n"):
            if line.strip():
                answer_lines.append(f"  {line}")
            else:
                answer_lines.append("")
        indented_answer = "\n".join(answer_lines)

        # Get code template
        template = problem.get("code_template", "").strip()

        # Indent template lines for YAML literal block scalar
        template_lines = []
        for line in template.split("\n"):
            if line.strip():
                template_lines.append(f"  {line}")
            else:
                template_lines.append("")
        indented_template = "\n".join(template_lines)

        # Build YAML content
        yaml_content = f"problem: |\n{indented}\n\nanswer: |\n{indented_answer}\n"
        if template:
            yaml_content += f"\nanswer_template: |\n{indented_template}\n"

        output_path = os.path.join(output_dir, filename)
        with open(output_path, "w") as f:
            f.write(yaml_content)

        print(f"Written: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <json_file>")
        sys.exit(1)
    json_to_yamls(sys.argv[1])
