# 纯记忆服务与 app_id

记忆服务只存、召回、纠正、删除记忆。它不登录、不刷新、不登出，也不解析业务发来的令牌。

## 1. 为什么不验令牌

用户是谁，由天code 业务在调用前确认。业务把自己验过的 `user_id` 传进来：有 JSON body 的接口放在 body 里，GET 和 DELETE 用 `?user_id=`。缺了或是空串，接口返回 422。

记忆服务信任这个 `user_id`。它防的是「代码漏写用户条件」和「行级权限把别人的行挡住」，不防「调用方故意填一个别人的 user_id」。伪造身份是业务的责任，不是记忆服务的责任。练习机可以不配服务密钥；生产前如果要挡外网误调，另加可选的服务密钥，那也不代替业务传身份。

## 2. 「应用」和原来的接入方标识

原来用单独一列表示接入方，默认还是 `tiancode`，而且令牌里的 `app` 可以把它改成别的值。产品上没有多应用，也不做应用切换。

现在应用标识只来自服务配置 `APP_ID`（默认 `tiancode`），列名是 `app_id`。调用方在 body 里写 `app_id` 会被拒绝（422）。配置值必须是 1 到 64 位小写字母、数字、`-` 或 `_`。

## 3. 为什么改名要分两次发布

练习机上已经有数据，列不能一次改名。第一次发布（PR1）：

1. 新迁移给每张相关表加上 `app_id`，数据库默认值是 `tiancode`，再按旧的接入方标识列回填，然后改成非空。
2. 按 `app_id` 建并行的索引和唯一约束。旧列、旧索引先留着。
3. 程序写入时两列写成同一个值；读取和过滤只用 `app_id`。

`server_default='tiancode'` 是为了回滚：如果新代码出问题，退回旧镜像，旧镜像的 INSERT 不带 `app_id`，数据库仍能用默认值把行写进去。等练习机稳定、确认不再需要旧镜像之后，第二次发布才删掉旧列和旧约束。一次删干净的话，旧镜像对不上新表，回滚会写失败。

## 4. PR2：删列与回滚

第二次发布删掉旧的接入方标识列，以及只服务那一列的索引和唯一约束。库里只留 `app_id`。

合并之后，上一版镜像不能直接对着新表写：旧代码还会读写已经不存在的列。**若必须退回 PR1 镜像，先执行 `alembic downgrade 0005_app_id`。** downgrade 会把旧列加回来，并用当前的 `app_id` 回填，PR1 镜像才能继续读写。确认新版本没问题之后，不要再降级。

## 5. 练习

在练习机的 `psql` 里执行：

```sql
SELECT table_name
FROM information_schema.columns
WHERE table_schema = 'public' AND column_name = 'app_id'
ORDER BY table_name;
```

`session_message`、`memory_fact`、`memory_preference`、`session_summary`、`memory_job`、`audit_log`、`user_setting` 都应出现。再查这些表，不应再有旧的接入方标识列；`memory_fact.app_id` 应都是配置里的 `APP_ID`（默认 `tiancode`），除非这行是从更早的数据回填来的。
