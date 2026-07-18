# 升级 LiteLLM

1. 只接受正式版 `vX.Y.Z`，拒绝 latest/main/rc/nightly/dev  
2. `sh scripts/sync-upstream.sh vX.Y.Z`  
3. 更新 `config/versions.env`  
4. `pytest tests/unit tests/contract -q`  
5. 阅读 release notes  
6. 重建镜像并 smoke  
7. 使用 Postgres 时先 `sh scripts/backup-db.sh`，并保持 `LITELLM_SALT_KEY` 不变  

详见 `scripts/upgrade.sh`。
