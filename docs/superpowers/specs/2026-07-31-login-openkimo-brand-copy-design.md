# OpenKimo 登录页品牌副文案

## 目标

将登录页的品牌副文案从 `Enter your credentials to access Kimo Code` 改为 `Enter your credentials to access OpenKimo`，使登录页与产品名称一致。

## 范围

仅修改 `kimo-cli/web/src/features/auth/login-page.tsx` 中登录卡片的这条副文案。保留现有组件、布局、样式、表单行为和其余登录页面文案不变。

不引入 i18n，不改登录页其他品牌元素，也不修改任何后端、认证或打包逻辑。

## 验证

先新增或扩展一个适合的前端源码合同测试：它必须断言旧字符串不存在，并断言新字符串存在。随后运行前端 typecheck 与 build；二者均须通过。

## 保护项

不得改动当前工作树中未跟踪的 `tests/test_wiki_approval_verification.py` 与 `tests/test_wiki_independent_verification.py`，也不得触碰主工作树的未提交改动。
