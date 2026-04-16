# File location: /server/tools/openai/validator.py
import asyncio
from typing import List, Optional, Union


async def validate_response(schema: dict, response: Optional[Union[list, dict]]) -> bool:
    """
    Validate that the response matches the provided schema.
    Specifically checks that "final_result" is present and meets the schema.
    """

    async def validate_item(
        schema_item: dict,
        item: Union[dict, list, str, int, float, bool],
        parent: dict = None
    ) -> bool:
        """
        Validate a single item (string, number, dict, list, etc.) against schema_item.
        """
        item_type = schema_item.get("type")

        # 1) If schema_item is an object
        if item_type == "object":
            if not isinstance(item, dict):
                return False
            return await validate_dict(schema_item.get("keys", []), item, parent)

        # 2) If schema_item is a list
        elif item_type == "list":
            if not isinstance(item, list):
                return False
            item_schema = schema_item.get("values")
            if not item_schema:
                # If there's no sub-schema for the list items, we accept any list
                return True
            # Validate every sub-item
            results = await asyncio.gather(
                *(validate_item(item_schema, sub_item, parent) for sub_item in item)
            )
            return all(results)

        # 3) If there's an enum, check membership
        elif "enum" in schema_item:
            return item in schema_item["enum"]

        # 4) Otherwise, treat as a primitive
        else:
            # Handle 'any' type explicitly
            if item_type == "any":
                # If anything is acceptable, just return True
                return True

            type_map = {
                "string": str,
                "integer": int,
                "float": (float, int),
                "boolean": bool,
            }
            expected_type = type_map.get(item_type)
            if expected_type is None:
                # If it's neither a known type nor 'any', fail or raise an error.
                raise ValueError(f"Unrecognized item_type: {item_type}")

            return isinstance(item, expected_type)

    async def validate_dict(schema_keys: List[dict], obj: dict, parent: dict = None) -> bool:
        """
        Validate a dict 'obj' against a list of schema_keys.
        Also handles 'conditions' on each schema key.
        """
        # Collect all the schema-defined key names
        schema_key_names = {
            key_schema["name"] for key_schema in schema_keys if "name" in key_schema
        }

        for key_schema in schema_keys:
            key_name = key_schema.get("name")
            required = key_schema.get("required", False)

            # Skip any malformed schema entries with no "name"
            if not key_name:
                continue

            # If required and not present => fail
            if key_name not in obj:
                if required:
                    return False
                # else it's optional => skip further checks
                continue

            value = obj[key_name]

            # Allow null for optional fields — LLMs commonly return null
            # for unused optional fields (e.g. "correction_key": null)
            if value is None and not required:
                continue

            # --- CONDITIONAL LOGIC ---
            if "conditions" in key_schema:
                for condition in key_schema["conditions"]:
                    if_condition = condition.get("if", {})
                    then_rules = condition.get("then", [])
                    if if_condition and parent:
                        # If the parent's key matches the condition
                        if parent.get(if_condition["key"]) == if_condition["value"]:
                            # Apply the 'then' rules
                            for rule in then_rules:
                                rule_key = rule.get("name")
                                rule_required = rule.get("required", False)

                                if isinstance(value, dict):
                                    if rule_required and rule_key not in value:
                                        return False
                                else:
                                    if rule_required:
                                        return False
            # --- END CONDITIONS ---

            # Validate the item itself (recursively if needed)
            if not await validate_item(key_schema, value, obj):
                return False

        # Ignore extra keys in the object
        for key in obj:
            if key not in schema_key_names:
                print(f"INFO: Skipping unknown key '{key}' in object.")

        return True

    # Must have "final_result" in the top-level response
    if not isinstance(response, dict) or "final_result" not in response:
        return False

    # Validate final_result with the top-level schema
    return await validate_item(schema, response["final_result"])
