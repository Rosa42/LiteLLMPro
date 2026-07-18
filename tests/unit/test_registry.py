from shared_quota_router.registry import registry_from_model_list


def test_registry_reads_model_info_and_pick_probe() -> None:
    model_list = [
        {
            "model_name": "kimi-k3",
            "model_info": {
                "deployment_id": "opencode-a-kimi",
                "provider_id": "opencode-go",
                "quota_group_id": "opencode-a",
                "priority": 10,
            },
            "litellm_params": {
                "model": "openai/kimi-k3",
                "api_key": "os.environ/OPENCODE_GO_KEY_A",
            },
        },
        {
            "model_name": "glm-5.2",
            "model_info": {
                "deployment_id": "opencode-a-glm",
                "provider_id": "opencode-go",
                "quota_group_id": "opencode-a",
                "priority": 10,
            },
            "litellm_params": {"model": "openai/glm-5.2"},
        },
        {
            "model_name": "kimi-k3",
            "model_info": {
                "deployment_id": "opencode-b-kimi",
                "provider_id": "opencode-go",
                "quota_group_id": "opencode-b",
                "priority": 20,
            },
            "litellm_params": {"model": "openai/kimi-k3"},
        },
    ]
    reg = registry_from_model_list(model_list)
    kimi = reg.get_by_model_group("kimi-k3")
    assert len(kimi) == 2
    a = reg.get_by_quota_group("opencode-a")
    assert {d.deployment_id for d in a} == {"opencode-a-kimi", "opencode-a-glm"}
    probe = reg.pick_probe_deployment("opencode-a", preferred_model_groups=["kimi-k3"])
    assert probe is not None
    assert probe.deployment_id == "opencode-a-kimi"
    assert probe.api_key_env == "OPENCODE_GO_KEY_A"
