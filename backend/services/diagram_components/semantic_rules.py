"""语义连接规则引擎

场景驱动的端口匹配系统：
- 每个场景定义参与组件集合 + 多组端口绑定配置
- 每组配置有可读标签（如"铁圈高位"、"铁圈低位"），供用户/Ctrl+多选时挑选
- AI 生成图时传场景关键词 → 返回默认配置的端口映射
- 安全校验：规则库外的不合法组合被阻止
"""

ENABLE_SEMANTIC = True

# 加热场景中酒精灯火焰顶与容器底之间的视觉间距（px）。
# 与 component_db 中 alcohol_lamp 的 flame_top port 的 gap 字段保持一致。
HEAT_GAP = 8

# ── 场景规则库 ──
# 每个场景: { name, components_required(set of types), configs(list of {label, port_bindings}) }
# port_bindings: [(src_type, src_port, dst_type, dst_port), ...]
# 第一个 config 为默认配置

SEMANTIC_SCENES = {
    # ═══ 加热液体类 ═══
    "heating_liquid": {
        "name": "加热液体",
        "components_required": {"alcohol_lamp", "tripod", "wire_gauze"},
        "configs": [
            {
                "label": "标准加热（三脚架+石棉网）",
                "port_bindings": [
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                    ("wire_gauze", "bottom", "tripod", "top"),
                ],
            },
        ],
    },
    "heating_beaker": {
        "name": "加热烧杯",
        "components_required": {"alcohol_lamp", "tripod", "wire_gauze", "beaker"},
        "configs": [
            {
                "label": "烧杯加热（石棉网+三脚架）",
                "port_bindings": [
                    ("beaker", "bottom", "wire_gauze", "top"),
                    ("wire_gauze", "bottom", "tripod", "top"),
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                ],
            },
        ],
    },
    "heating_conical": {
        "name": "加热锥形瓶",
        "components_required": {"alcohol_lamp", "tripod", "wire_gauze", "conical_flask"},
        "configs": [
            {
                "label": "锥形瓶加热（石棉网+三脚架）",
                "port_bindings": [
                    ("conical_flask", "bottom", "wire_gauze", "top"),
                    ("wire_gauze", "bottom", "tripod", "top"),
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                ],
            },
        ],
    },
    "heating_test_tube": {
        "name": "加热试管",
        "components_required": {"alcohol_lamp", "test_tube", "iron_stand"},
        "configs": [
            {
                "label": "试管加热（铁架台夹持+酒精灯）",
                "port_bindings": [
                    ("test_tube", "bottom", "alcohol_lamp", "flame_top"),
                    ("iron_stand", "clamp", "test_tube", "body"),
                ],
            },
        ],
    },
    "heating_round_flask": {
        "name": "加热圆底烧瓶",
        "components_required": {"alcohol_lamp", "tripod", "wire_gauze", "round_bottom_flask"},
        "configs": [
            {
                "label": "圆底烧瓶加热（石棉网+三脚架）",
                "port_bindings": [
                    ("round_bottom_flask", "bottom", "wire_gauze", "top"),
                    ("wire_gauze", "bottom", "tripod", "top"),
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                ],
            },
        ],
    },
    "heating_crucible": {
        "name": "加热坩埚",
        "components_required": {"alcohol_lamp", "tripod", "crucible"},
        "configs": [
            {
                "label": "坩埚直接加热（三脚架）",
                "port_bindings": [
                    ("crucible", "bottom", "tripod", "top"),
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                ],
            },
        ],
    },
    "heating_evaporating_dish": {
        "name": "加热蒸发皿",
        "components_required": {"alcohol_lamp", "tripod", "evaporating_dish"},
        "configs": [
            {
                "label": "蒸发皿加热（三脚架）",
                "port_bindings": [
                    ("evaporating_dish", "top", "tripod", "top"),
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                ],
            },
        ],
    },
    "water_bath_heating": {
        "name": "水浴加热",
        "components_required": {"alcohol_lamp", "tripod", "wire_gauze", "water_bath"},
        "configs": [
            {
                "label": "水浴加热（水浴锅+三脚架）",
                "port_bindings": [
                    ("water_bath", "bottom", "wire_gauze", "top"),
                    ("wire_gauze", "bottom", "tripod", "top"),
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                ],
            },
        ],
    },

    # ═══ 铁架台多配置（关键：同一组件对，多种连接方式） ═══
    "iron_stand_clamp": {
        "name": "铁架台夹持装置",
        "components_required": {"iron_stand"},
        "configs": [
            {
                "label": "铁夹高位",
                "port_bindings": [],
                "params": {"clamp_y": 18, "clamp_w": 30, "has_clamp": True},
                "params_target": "iron_stand",
            },
            {
                "label": "铁夹中位",
                "port_bindings": [],
                "params": {"clamp_y": 40, "clamp_w": 30, "has_clamp": True},
                "params_target": "iron_stand",
            },
            {
                "label": "铁夹低位",
                "port_bindings": [],
                "params": {"clamp_y": 62, "clamp_w": 30, "has_clamp": True},
                "params_target": "iron_stand",
            },
        ],
    },
    "iron_stand_ring": {
        "name": "铁架台铁圈",
        "components_required": {"iron_stand"},
        "configs": [
            {
                "label": "铁圈高位（过滤/蒸馏）",
                "port_bindings": [],
                "params": {"clamp_y": 22, "clamp_w": 28, "has_ring": True},
                "params_target": "iron_stand",
            },
            {
                "label": "铁圈中位+酒精灯摆法",
                "port_bindings": [
                    ("alcohol_lamp", "flame_top", "iron_stand", "ring_bottom"),
                ],
                "params": {"clamp_y": 42, "clamp_w": 28, "has_ring": True},
                "params_target": "iron_stand",
            },
            {
                "label": "铁圈中位+烧杯摆法",
                "port_bindings": [
                    ("beaker", "bottom", "iron_stand", "ring_top"),
                ],
                "params": {"clamp_y": 42, "clamp_w": 28, "has_ring": True},
                "params_target": "iron_stand",
            },
            {
                "label": "铁圈低位",
                "port_bindings": [],
                "params": {"clamp_y": 60, "clamp_w": 28, "has_ring": True},
                "params_target": "iron_stand",
            },
        ],
    },

    # ═══ 蒸馏类 ═══
    "distillation": {
        "name": "蒸馏装置",
        "components_required": {"alcohol_lamp", "round_bottom_flask", "distillation_head",
                               "condenser", "cow_receiver", "conical_flask",
                               "iron_stand", "thermometer", "tripod", "wire_gauze"},
        "configs": [
            {
                "label": "标准蒸馏装置",
                "port_bindings": [
                    ("round_bottom_flask", "bottom", "wire_gauze", "top"),
                    ("wire_gauze", "bottom", "tripod", "top"),
                    ("alcohol_lamp", "flame_top", "tripod", "bottom"),
                    ("distillation_head", "bottom", "round_bottom_flask", "top"),
                    ("thermometer", "bottom", "distillation_head", "top"),
                    ("distillation_head", "side", "condenser", "top"),
                    ("condenser", "bottom", "cow_receiver", "top"),
                    ("cow_receiver", "bottom", "conical_flask", "top"),
                ],
            },
        ],
    },

    # ═══ 过滤类 ═══
    "filtration": {
        "name": "过滤装置",
        "components_required": {"funnel", "beaker", "iron_stand", "glass_tube"},
        "configs": [
            {
                "label": "标准过滤（玻璃棒引流）",
                "port_bindings": [
                    ("funnel", "bottom", "beaker", "top"),
                    ("glass_tube", "bottom", "funnel", "top"),
                ],
            },
        ],
    },
    "filtration_stand": {
        "name": "过滤（铁架台+漏斗）",
        "components_required": {"funnel", "beaker", "iron_stand"},
        "configs": [
            {
                "label": "铁架台支撑漏斗",
                "port_bindings": [
                    ("iron_stand", "clamp", "funnel", "body"),
                    ("funnel", "bottom", "beaker", "top"),
                ],
            },
        ],
    },

    # ═══ 集气类 ═══
    "gas_collection_water": {
        "name": "排水集气",
        "components_required": {"water_tank", "gas_bottle", "delivery_tube", "test_tube"},
        "configs": [
            {
                "label": "排水集气法（标准）",
                "port_bindings": [
                    ("delivery_tube", "right", "gas_bottle", "top"),
                    ("test_tube", "top", "gas_bottle", "bottom"),
                ],
            },
        ],
    },
    "gas_collection_upward": {
        "name": "向上排空气集气",
        "components_required": {"gas_bottle", "delivery_tube"},
        "configs": [
            {
                "label": "向上排空气法",
                "port_bindings": [
                    ("delivery_tube", "right", "gas_bottle", "bottom"),
                ],
            },
        ],
    },
    "gas_collection_downward": {
        "name": "向下排空气集气",
        "components_required": {"gas_bottle", "delivery_tube"},
        "configs": [
            {
                "label": "向下排空气法",
                "port_bindings": [
                    ("delivery_tube", "right", "gas_bottle", "top"),
                ],
            },
        ],
    },

    # ═══ 量取类 ═══
    "measure_liquid": {
        "name": "量取液体",
        "components_required": {"graduated_cylinder"},
        "configs": [
            {
                "label": "量筒读数",
                "port_bindings": [],
                "params": {"liquid": 0.5},
            },
        ],
    },
    "dropper_add": {
        "name": "滴管加液",
        "components_required": {"dropper", "test_tube"},
        "configs": [
            {
                "label": "滴管滴入试管",
                "port_bindings": [
                    ("dropper", "tip", "test_tube", "top"),
                ],
            },
        ],
    },

    # ═══ 分液类 ═══
    "separatory": {
        "name": "分液操作",
        "components_required": {"separatory_funnel", "iron_stand", "beaker"},
        "configs": [
            {
                "label": "分液漏斗分液",
                "port_bindings": [
                    ("separatory_funnel", "bottom", "beaker", "top"),
                ],
            },
        ],
    },

    # ═══ 电解类 ═══
    "electrolysis": {
        "name": "电解水",
        "components_required": {"water_tank", "test_tube", "glass_tube"},
        "configs": [
            {
                "label": "电解水装置（双试管收集）",
                "port_bindings": [],
                "params": {
                    "test_tube_count": 2,
                    "layout": "dual_tube_in_tank",
                },
            },
        ],
    },

    # ═══ 物理实验 ═══
    "pulley_system": {
        "name": "滑轮组",
        "components_required": {"pulley", "block"},
        "configs": [
            {
                "label": "定滑轮提升重物",
                "port_bindings": [
                    ("pulley", "bottom_left", "block", "top"),
                ],
            },
        ],
    },
    "inclined_plane_exp": {
        "name": "斜面实验",
        "components_required": {"inclined_plane", "block", "spring"},
        "configs": [
            {
                "label": "斜面+弹簧测力",
                "port_bindings": [
                    ("block", "right", "spring", "bottom"),
                ],
            },
        ],
    },
    "pendulum_exp": {
        "name": "单摆实验",
        "components_required": {"pendulum"},
        "configs": [
            {
                "label": "单摆（15度小角摆动）",
                "port_bindings": [],
                "params": {"angle": 15},
            },
        ],
    },
}


# ── 安全校验：非法连接的阻止规则 ──
# 格式: [(src_type, dst_type, reason), ...]
# 如果某连接不在任何场景的合法 port_bindings 中但 attempted，触发 warning
FORBIDDEN_CONNECTIONS = [
    ("alcohol_lamp", "wire_gauze", "酒精灯不可直接接触石棉网，需通过三脚架支撑"),
    ("alcohol_lamp", "beaker", "酒精灯不可直接加热烧杯，需通过三脚架+石棉网"),
    ("alcohol_lamp", "conical_flask", "酒精灯不可直接加热锥形瓶"),
    ("alcohol_lamp", "round_bottom_flask", "酒精灯不可直接加热圆底烧瓶"),
    ("thermometer", "alcohol_lamp", "温度计不可直接接触酒精灯火焰"),
]


def match_scene(component_types: set[str]) -> list[dict]:
    """根据组件集合匹配适用的语义场景

    Args:
        component_types: 图中已放置的组件类型集合（如 {"beaker", "alcohol_lamp", ...}）

    Returns:
        [{"scene_id": "heating_beaker", "name": "加热烧杯",
          "configs": [{"label": "...", "port_bindings": [...], "params": {...}}, ...],
          "match_score": 0.8}, ...]
        按匹配分数降序排列
    """
    if not ENABLE_SEMANTIC:
        return []

    results = []
    for scene_id, scene in SEMANTIC_SCENES.items():
        required = scene["components_required"]
        if not required:
            continue
        # 匹配分数：已放置组件 ∩ 场景所需 / 场景所需
        matched = component_types & required
        if not matched:
            continue
        score = len(matched) / len(required)
        if score >= 0.5:  # 至少匹配一半
            results.append({
                "scene_id": scene_id,
                "name": scene["name"],
                "configs": scene["configs"],
                "match_score": round(score, 2),
                "missing": list(required - component_types),
            })

    results.sort(key=lambda r: r["match_score"], reverse=True)
    return results


def get_scene_config(scene_id: str, config_label: str = "") -> dict | None:
    """获取特定场景的指定配置"""
    if not ENABLE_SEMANTIC:
        return None

    scene = SEMANTIC_SCENES.get(scene_id)
    if not scene:
        return None

    # 归一化配置标签：去头尾空格、统一常用括号为全角
    norm_label = config_label.strip().replace("(", "（").replace(")", "）")
    if norm_label:
        for cfg in scene["configs"]:
            cfg_norm = cfg["label"].strip().replace("(", "（").replace(")", "）")
            if cfg_norm == norm_label:
                return cfg

    # 返回默认（第一个）
    return scene["configs"][0] if scene["configs"] else None


def get_scene_by_keyword(keyword: str) -> list[dict]:
    """按关键词搜索场景（供 AI 使用）"""
    if not ENABLE_SEMANTIC:
        return []
    results = []
    for scene_id, scene in SEMANTIC_SCENES.items():
        if keyword in scene["name"] or keyword in scene_id:
            # 只返回场景摘要（不含完整 configs，避免 token 过大）
            results.append({
                "scene_id": scene_id,
                "name": scene["name"],
                "components_required": list(scene["components_required"]),
                "config_count": len(scene["configs"]),
                "default_config": scene["configs"][0]["label"] if scene["configs"] else "",
            })
    return results


def resolve_port_bindings(scene_id: str, config_label: str = "",
                          components: list[dict] | None = None) -> dict:
    """解析场景配置，返回可用的端口绑定 + 组件参数"""
    if not ENABLE_SEMANTIC:
        return {"label": "", "port_bindings": [], "params": {}, "valid": False,
                "warnings": ["语义连接系统已关闭"]}
    config = get_scene_config(scene_id, config_label)
    if not config:
        return {"label": "", "port_bindings": [], "params": {}, "valid": False,
                "warnings": [f"场景 '{scene_id}' 不存在"]}

    result = {
        "label": config.get("label", ""),
        "port_bindings": list(config.get("port_bindings", [])),
        "params": {},
        "params_target": config.get("params_target", ""),
        "valid": True,
        "warnings": [],
    }

    # 构建组件类型集合用于校验
    comp_types = set()
    if components:
        for c in components:
            comp_types.add(c.get("type", ""))

    # 检查每个 port_binding 中的类型是否在 components 中存在
    if components:
        for binding in result["port_bindings"]:
            src_type, _, dst_type, _ = binding
            if src_type not in comp_types:
                result["warnings"].append(f"缺少连接源组件: {src_type}")
            if dst_type not in comp_types:
                result["warnings"].append(f"缺少连接目标组件: {dst_type}")

    # 提取 params（按组件类型分组）；dict() 拷贝避免污染全局场景库
    scene = SEMANTIC_SCENES.get(scene_id, {})
    for cfg in scene.get("configs", []):
        if cfg.get("label") == config.get("label"):
            if "params" in cfg:
                result["params"] = dict(cfg["params"])
            break

    if result["warnings"]:
        result["valid"] = False

    return result


def check_connection_safety(src_type: str, dst_type: str) -> dict:
    """检查连接是否安全"""
    if not ENABLE_SEMANTIC:
        return {"safe": True, "reason": "语义系统已关闭"}
    for f_src, f_dst, reason in FORBIDDEN_CONNECTIONS:
        if (f_src == src_type and f_dst == dst_type) or \
           (f_src == dst_type and f_dst == src_type):
            return {"safe": False, "reason": reason}
    return {"safe": True, "reason": ""}


def list_all_scenes() -> list[dict]:
    """列出所有可用语义场景（供前端/AI查询）。"""
    if not ENABLE_SEMANTIC:
        return []
    results = []
    for scene_id, scene in SEMANTIC_SCENES.items():
        results.append({
            "scene_id": scene_id,
            "name": scene["name"],
            "components": list(scene["components_required"]),
            "configs": [
                {"label": cfg["label"],
                 "binding_count": len(cfg.get("port_bindings", [])),
                 "has_params": "params" in cfg and bool(cfg["params"]),
                 # 供前端逐对调用连接安全检查，不输出端口级细节
                 "binding_pairs": [
                     {"src": binding[0], "dst": binding[2]}
                     for binding in cfg.get("port_bindings", [])
                     if isinstance(binding, (list, tuple)) and len(binding) >= 4
                 ]}
                for cfg in scene["configs"]
            ],
        })
    return results
