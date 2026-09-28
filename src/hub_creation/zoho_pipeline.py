"""Zohoが既定と明示した案件パイプラインだけを採用する。"""
from src.hub_creation.domain import CreationHeld


def default_pipeline(layout_pipelines, stage):
    """API応答だけで判定する。候補の先頭や表示名から業務上の所属を推測しない。"""
    defaults = []
    seen = set()
    for layout_id, pipelines in layout_pipelines:
        if not isinstance(layout_id, str) or not layout_id.isdigit() or not isinstance(pipelines, list):
            raise CreationHeld("案件パイプラインの設定を確認できません")
        for pipeline in pipelines:
            if (not isinstance(pipeline, dict) or not isinstance(pipeline.get("default"), bool)
                    or not isinstance(pipeline.get("id"), str) or not pipeline["id"].isdigit()
                    or pipeline["id"] in seen):
                raise CreationHeld("案件パイプラインの設定を確認できません")
            seen.add(pipeline["id"])
            if pipeline["default"]:
                defaults.append((layout_id, pipeline))
    if len(defaults) != 1:
        raise CreationHeld("既定の案件パイプラインが一意ではありません。Zohoの設定を確認してください")
    layout_id, pipeline = defaults[0]
    name, stages = pipeline.get("actual_value"), pipeline.get("maps")
    if (not isinstance(name, str) or not name.strip() or name == "-None-"
            or not isinstance(stages, list) or not stages
            or any(not isinstance(item, dict) or not isinstance(item.get("actual_value"), str) for item in stages)):
        raise CreationHeld("既定の案件パイプラインの設定を確認できません")
    if not isinstance(stage, str) or stage not in {item["actual_value"] for item in stages}:
        raise CreationHeld("営業ステータスがZohoの既定パイプラインに含まれません。設定または入力を確認してください")
    return name, layout_id
