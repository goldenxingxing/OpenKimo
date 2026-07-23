# Gitee 仓库镜像与 Release 同步设计

## 目标

让 GitHub 仓库 `goldenxingxing/OpenKimo` 成为唯一发布源，并自动将以下内容同步到 Gitee 仓库 `qunwei/OpenKimo`：

- Git 分支、标签及其他正常仓库 refs；
- GitHub Release 的标签、标题和说明；
- GitHub Release 中的 macOS ARM64、macOS Intel 和 Windows x64 安装包，以及以后新增的其他附件。

Gitee 同步失败不得影响 GitHub 的三平台构建和发布结果。

## 工作流边界

继续使用独立的 `.github/workflows/sync-to-gitee.yml`，不把 Gitee API 调用加入 `.github/workflows/release.yml`。

工作流承担两个独立职责：

1. Git 镜像：使用 SSH 将 GitHub 仓库镜像推送到 Gitee。
2. Release 同步：在 GitHub `Release` workflow 成功完成后，通过 API 将对应发行版及附件复制到 Gitee。

## 触发与并发

Git 镜像在以下事件发生时运行：

- `main` 分支 push；
- `v*` 标签 push；
- 分支或标签删除。

Release 同步由 `workflow_run` 监听名为 `Release` 的 GitHub workflow。只有上游运行结论为 `success`，且关联 ref 是 `v*` 标签时才运行。这样可以确保所有平台构建和附件上传都已结束。

为 Git 镜像和 Release 同步分别设置并发组，避免多个运行同时修改 Gitee 仓库或同一发行版。新运行不主动取消已开始的发布同步。

## 凭据

- `GITEE_RSA_PRIVATE_KEY`：供 SSH 镜像 Action 使用，必须能读取 GitHub 源仓库并写入 Gitee 目标仓库。
- `GITEE_ACCESS_TOKEN`：供 Gitee API 使用，必须具有目标仓库 Release 的读写权限。
- `GITHUB_TOKEN`：读取当前 GitHub 仓库的 Release 元数据和附件；由 GitHub Actions 自动提供。

秘密值不得输出到日志或写入构建产物。

## Release 同步流程

1. 从成功完成的 `workflow_run` 获取标签。
2. 调用 GitHub API读取该标签的 Release，包括标题、正文以及附件列表。
3. 在 Gitee 查询同标签 Release：
   - 不存在时创建；
   - 已存在时更新标题和正文。
4. 枚举 GitHub Release 的全部附件，而不是硬编码文件名。
5. 对每个附件：
   - 下载到 runner 临时目录；
   - 若 Gitee 已有同名附件，则删除旧附件；
   - 上传新附件。
6. 所有临时文件随 runner 销毁，不提交进仓库。

该流程设计为可重复执行。重新运行同一标签时，Gitee Release 最终内容应与 GitHub 一致，不产生重复附件。

## Git 镜像行为

镜像 Action 使用 `git push --mirror`，因此目标 Gitee 仓库仅作为 GitHub 的镜像。GitHub 不存在的 Gitee 独有 refs 可能被删除，这是预期行为。

同步配置监听标签创建和删除，避免只能等下一次 `main` 更新才补传标签。增加并发控制，避免两个镜像推送互相竞争。

## 错误处理

- 上游 Release workflow 失败或取消时，不创建 Gitee Release。
- 缺少标签、GitHub Release、令牌或 API 返回非成功状态时，job 明确失败。
- API 调用使用失败即退出、有限重试和明确的步骤名称，便于从 Actions 页面定位故障。
- Git 镜像与 Release 同步是不同 job；其中一个失败不会伪装成另一个成功。
- Gitee 同步失败不会改变 GitHub Release，也不会删除 GitHub 附件。

## 验证

静态验证包括：

- workflow YAML 可解析；
- GitHub Actions 表达式和事件字段有效；
- shell 脚本通过语法检查；
- workflow 不包含明文凭据；
- Release 同步仅在成功的 `v*` 发布运行后执行。

远端验收使用下一次测试标签或正式版本标签：

1. GitHub Release workflow 完成；
2. Gitee 出现相同标签；
3. Gitee Release 标题和说明与 GitHub 一致；
4. Gitee Release 附件名称和数量与 GitHub 一致；
5. 重新运行同步不会产生重复附件。
