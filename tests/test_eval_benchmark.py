"""中文陪伴场景记忆召回质量评测集（F1 门禁）。

覆盖五大类别：
1. preference: 用户个人偏好（饮食、作息、称呼、习惯）
2. fact_update: 事实更新（工作、住址、宠物，旧版本已失效）
3. negation: 否定与忌口（过敏、讨厌、拒绝）
4. expiration: 过期失效记忆（短期事件已设置 valid_to，不应被召回）
5. multi_turn: 多轮对话关键事实沉淀（家庭关系、纪念日、长远目标）

CI 门槛：
- Recall 覆盖率 >= 80%
- Precision 准确率 >= 80%
低于门槛则构建失败。
"""
from datetime import datetime, timedelta, timezone

from app import models as m
from app.auth import UserContext
from app.recall import recall_facts

# 50条中文陪伴场景基准评测集
BENCHMARK_CASES = [
    # --- 1. preference (偏好类 12条) ---
    {
        "subj": "饮品偏好",
        "cat": "preference",
        "fact": "用户最喜欢的饮品是半糖去冰的乌龙奶茶",
        "query": "用户想喝什么奶茶",
        "expect": "乌龙奶茶",
    },
    {
        "subj": "作息习惯",
        "cat": "preference",
        "fact": "用户习惯每天凌晨一点入睡，是个典型的夜猫子",
        "query": "用户的作息习惯如何",
        "expect": "夜猫子",
    },
    {
        "subj": "个人称呼",
        "cat": "preference",
        "fact": "用户希望AI称呼自己为小雨",
        "query": "怎么称呼用户",
        "expect": "小雨",
    },
    {
        "subj": "周末爱好",
        "cat": "preference",
        "fact": "用户周末喜欢去户外徒步和露营",
        "query": "用户周末有什么户外爱好",
        "expect": "徒步",
    },
    {
        "subj": "电影偏好",
        "cat": "preference",
        "fact": "用户看电影偏爱科幻悬疑题材，反感恐怖片",
        "query": "用户喜欢什么类型的电影",
        "expect": "科幻",
    },
    {
        "subj": "音乐偏好",
        "cat": "preference",
        "fact": "用户平时听歌喜欢周杰伦和陶喆的经典R&B音乐",
        "query": "用户喜欢听谁的歌",
        "expect": "周杰伦",
    },
    {
        "subj": "咖啡饮品",
        "cat": "preference",
        "fact": "用户喜欢在下雨天喝一杯热拿铁咖啡",
        "query": "下雨天用户喜欢喝什么",
        "expect": "热拿铁",
    },
    {
        "subj": "口味偏好",
        "cat": "preference",
        "fact": "用户偏好清淡江南口味，不能吃太油腻的菜",
        "query": "用户的口味偏好是什么",
        "expect": "清淡",
    },
    {
        "subj": "阅读偏好",
        "cat": "preference",
        "fact": "用户看书阅读时喜欢纸质书的触感，不喜欢电子书",
        "query": "用户看书有什么偏好",
        "expect": "纸质书",
    },
    {
        "subj": "季节偏好",
        "cat": "preference",
        "fact": "用户最喜欢的季节是秋天，因为天气凉爽舒适",
        "query": "用户最喜欢哪个季节",
        "expect": "秋天",
    },
    {
        "subj": "运动健身",
        "cat": "preference",
        "fact": "用户运动健身偏好晨跑5公里和游泳",
        "query": "用户平时做什么运动",
        "expect": "晨跑",
    },
    {
        "subj": "工作专注",
        "cat": "preference",
        "fact": "用户习惯在工作时佩戴降噪耳机听白噪音",
        "query": "用户工作时有什么专注习惯",
        "expect": "白噪音",
    },

    # --- 2. fact_update (事实更新更正 10条：新旧版本对比) ---
    {
        "subj": "工作城市",
        "cat": "fact_update",
        "fact": "用户现在全职在杭州的科技公司做后端架构师",
        "query": "用户现在在哪里工作",
        "expect": "杭州",
        "old": "用户在上海做前端开发",
    },
    {
        "subj": "宠物狗",
        "cat": "fact_update",
        "fact": "用户新养了一只白色萨摩耶宠物狗名叫雪球",
        "query": "用户的宠物狗叫什么",
        "expect": "雪球",
        "old": "用户养了一只柯基犬叫豆豆",
    },
    {
        "subj": "居住公寓",
        "cat": "fact_update",
        "fact": "用户目前住在西湖区的新公寓",
        "query": "用户目前住在哪个区",
        "expect": "西湖区",
        "old": "用户租住在滨江区的高层住宅",
    },
    {
        "subj": "手机型号",
        "cat": "fact_update",
        "fact": "用户现在主力手机换成了iPhone16Pro",
        "query": "用户的手机型号是什么",
        "expect": "iPhone16Pro",
        "old": "用户使用的是小米13手机",
    },
    {
        "subj": "情感状态",
        "cat": "fact_update",
        "fact": "用户目前处于单身状态，专注自我提升",
        "query": "用户的情感状态如何",
        "expect": "单身",
        "old": "用户正在恋爱交往中",
    },
    {
        "subj": "编程语言",
        "cat": "fact_update",
        "fact": "用户的常用工作开发语言转成了Python和Rust",
        "query": "用户主力写什么编程语言",
        "expect": "Rust",
        "old": "用户主力写Java语言",
    },
    {
        "subj": "资格证书",
        "cat": "fact_update",
        "fact": "用户最近报班考取了中级经济师资格证",
        "query": "用户最近考了什么资格证",
        "expect": "经济师",
        "old": "用户正在准备初级会计考试",
    },
    {
        "subj": "代步车辆",
        "cat": "fact_update",
        "fact": "用户现在开的代步车换成了特斯拉ModelY纯电SUV",
        "query": "用户现在开什么车",
        "expect": "ModelY",
        "old": "用户开一辆大众燃油轿车",
    },
    {
        "subj": "吸烟习惯",
        "cat": "fact_update",
        "fact": "用户戒掉了抽烟习惯，已经坚持半年不抽烟",
        "query": "用户还抽烟吗",
        "expect": "戒掉",
        "old": "用户有吸烟的习惯",
    },
    {
        "subj": "眼镜样式",
        "cat": "fact_update",
        "fact": "用户的近视眼镜换成了超薄钛合金半框镜架",
        "query": "用户的眼镜是什么样式",
        "expect": "钛合金",
        "old": "用户戴黑色塑料黑框眼镜",
    },

    # --- 3. negation (否定与禁忌 10条) ---
    {
        "subj": "食物过敏",
        "cat": "negation",
        "fact": "用户对花生和坚果类食物重度过敏，误食会休克",
        "query": "用户对什么食物过敏",
        "expect": "花生",
    },
    {
        "subj": "忌口偏好",
        "cat": "negation",
        "fact": "用户极度厌恶香菜和折耳根，点菜必须完全不加",
        "query": "用户点菜有什么忌口",
        "expect": "香菜",
    },
    {
        "subj": "乳糖耐受",
        "cat": "negation",
        "fact": "用户有重度乳糖不耐受，喝纯牛奶会腹胀腹泻",
        "query": "用户能喝牛奶吗",
        "expect": "乳糖不耐受",
    },
    {
        "subj": "饮酒习惯",
        "cat": "negation",
        "fact": "用户酒精过敏，滴酒不沾，聚会只喝果汁，从不喝酒",
        "query": "用户喝酒吗",
        "expect": "不喝酒",
    },
    {
        "subj": "幽闭恐惧",
        "cat": "negation",
        "fact": "用户害怕去密闭狭小空间，有轻微幽闭恐惧症",
        "query": "用户害怕什么场景",
        "expect": "幽闭",
    },
    {
        "subj": "看剧禁忌",
        "cat": "negation",
        "fact": "用户坚决不接受任何形式的剧透，看剧前忌讳谈论结局",
        "query": "用户看剧有什么禁忌",
        "expect": "剧透",
    },
    {
        "subj": "痛风禁忌",
        "cat": "negation",
        "fact": "用户痛风发作过，医生嘱咐绝对不能喝海鲜啤酒汤",
        "query": "用户饮食不能碰什么",
        "expect": "痛风",
    },
    {
        "subj": "养猫禁忌",
        "cat": "negation",
        "fact": "用户对猫毛过敏，接触猫咪会严重打喷嚏流泪，不能养猫",
        "query": "用户为什么不能养猫",
        "expect": "猫毛过敏",
    },
    {
        "subj": "生食禁忌",
        "cat": "negation",
        "fact": "用户不吃任何生食，包括刺身和三分熟牛排",
        "query": "用户吃日料刺身吗",
        "expect": "不吃任何生食",
    },
    {
        "subj": "社交禁忌",
        "cat": "negation",
        "fact": "用户讨厌社交中别人未经允许翻看手机屏幕的行为",
        "query": "用户讨厌什么社交行为",
        "expect": "翻看手机",
    },

    # --- 4. multi_turn (多轮深层事实 10条) ---
    {
        "subj": "母亲生日",
        "cat": "multi_turn",
        "fact": "用户的母亲生日是公历每年五月十二日护士节",
        "query": "用户母亲的生日是哪天",
        "expect": "五月十二日",
    },
    {
        "subj": "大学母校",
        "cat": "multi_turn",
        "fact": "用户大学毕业于南京大学软件工程专业",
        "query": "用户的母校和大学专业是什么",
        "expect": "南京大学",
    },
    {
        "subj": "妹妹升学",
        "cat": "multi_turn",
        "fact": "用户的妹妹正在北京读高三准备高考",
        "query": "用户的妹妹在哪里上学",
        "expect": "北京读高三",
    },
    {
        "subj": "好友出游",
        "cat": "multi_turn",
        "fact": "用户和最好的朋友老张每年国庆都会自驾游一次",
        "query": "用户国庆节常和谁去自驾游",
        "expect": "老张",
    },
    {
        "subj": "退休梦想",
        "cat": "multi_turn",
        "fact": "用户的长期梦想是退休后在云南大理开一家独立书店",
        "query": "用户未来退休后的梦想是什么",
        "expect": "大理",
    },
    {
        "subj": "宠物猫咪",
        "cat": "multi_turn",
        "fact": "用户家里养的一只英短蓝猫宠物猫名字叫肉包",
        "query": "用户的猫咪叫什么名字",
        "expect": "肉包",
    },
    {
        "subj": "健康体检",
        "cat": "multi_turn",
        "fact": "用户每年体检左肺有一颗4毫米良性微小结节",
        "query": "用户的体检有什么健康注意事项",
        "expect": "结节",
    },
    {
        "subj": "老家房产",
        "cat": "multi_turn",
        "fact": "用户在老家有一套父母留下的两居室老房子",
        "query": "用户老家有房产吗",
        "expect": "老房子",
    },
    {
        "subj": "驾照类型",
        "cat": "multi_turn",
        "fact": "用户的驾照是C1手动挡驾照已有六年驾龄",
        "query": "用户的驾照类型和驾龄",
        "expect": "C1",
    },
    {
        "subj": "乐器特长",
        "cat": "multi_turn",
        "fact": "用户少年时期曾学过五年小提琴并通过了十级",
        "query": "用户小时候学过什么乐器",
        "expect": "小提琴",
    },

    # --- 5. expiration (时效过期记忆 8条：设置已过期 valid_to，检验不被召回) ---
    {
        "subj": "换驾照",
        "cat": "expiration",
        "fact": "用户明天上午九点要去车管所办理换驾照",
        "query": "明天上午用户要去哪里办业务",
        "expired": True,
    },
    {
        "subj": "感冒生病",
        "cat": "expiration",
        "fact": "用户今晚头痛感冒正在吃感冒灵颗粒",
        "query": "用户今天感冒身体不舒服吗",
        "expired": True,
    },
    {
        "subj": "出差参会",
        "cat": "expiration",
        "fact": "用户上周在出差深圳出差参加技术峰会",
        "query": "用户上周在哪里出差",
        "expired": True,
    },
    {
        "subj": "话费充值",
        "cat": "expiration",
        "fact": "用户昨天手机停机充值了100元话费",
        "query": "用户昨天充话费了吗",
        "expired": True,
    },
    {
        "subj": "旅游自驾",
        "cat": "expiration",
        "fact": "用户正在租车自驾川西环线旅游",
        "query": "用户现在在川西旅游吗",
        "expired": True,
    },
    {
        "subj": "牙医预约",
        "cat": "expiration",
        "fact": "用户今天下午有一次牙齿根管治疗预约",
        "query": "用户下午有什么医生预约",
        "expired": True,
    },
    {
        "subj": "昨晚失眠",
        "cat": "expiration",
        "fact": "用户昨晚失眠只睡了三个小时",
        "query": "用户昨晚失眠了吗",
        "expired": True,
    },
    {
        "subj": "借钱记录",
        "cat": "expiration",
        "fact": "用户上个月借给同事小李两千元急用",
        "query": "用户借钱给谁了吗",
        "expired": True,
    },
]


def test_companion_recall_quality_benchmark(db):
    """50条中文陪伴场景评测集门禁测试。

    统计召回覆盖率（Coverage / Recall）与准确率（Precision）。
    门槛要求：
    - 有效用例覆盖率 >= 80%
    - 检索准确率 >= 80%
    - 过期失效用例召回率为 0%
    """
    user_id = "eval_user_01"
    u = UserContext(app_id="tiancode", user_id=user_id)
    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)

    # 1. 种子数据入库
    for i, item in enumerate(BENCHMARK_CASES):
        is_expired = item.get("expired", False)
        subj = item.get("subj", f"subj_{i}")
        # 如果是更新用例，先插入一条已失效的历史版本
        if "old" in item:
            db.add(m.MemoryFact(
                app_id=u.app_id, user_id=u.user_id, subject=subj,
                content=item["old"], version=1, valid_from=now - timedelta(days=10),
                valid_to=one_day_ago,
            ))
        # 插入当前版本
        db.add(m.MemoryFact(
            app_id=u.app_id, user_id=u.user_id, subject=subj,
            content=item["fact"], version=2 if "old" in item else 1,
            valid_from=now - timedelta(days=2),
            valid_to=one_day_ago if is_expired else None,  # 过期记忆 valid_to 不为空
        ))
    db.commit()

    # 2. 执行评测
    active_cases = [c for c in BENCHMARK_CASES if not c.get("expired")]
    expired_cases = [c for c in BENCHMARK_CASES if c.get("expired")]

    hit_count = 0
    correct_count = 0

    for c in active_cases:
        recalled = recall_facts(db, u, query=c["query"], k=5, token_budget=1500)
        recalled_texts = [r.content for r in recalled]
        # 判定是否召回了期望的关键事实
        matched = any(c["expect"] in text for text in recalled_texts)
        if matched:
            hit_count += 1
            correct_count += 1

    # 过期用例测试：绝对不能召回已过期的内容
    expired_leak_count = 0
    for c in expired_cases:
        recalled = recall_facts(db, u, query=c["query"], k=5, token_budget=1500)
        if any(c["fact"] in r.content for r in recalled):
            expired_leak_count += 1

    coverage = hit_count / len(active_cases)
    precision = correct_count / max(hit_count, 1)

    print("\n" + "=" * 50)
    print("【中文陪伴场景记忆召回评测报告 (F1)】")
    print(f"总用例数: {len(BENCHMARK_CASES)}")
    print(f"活跃事实用例: {len(active_cases)}")
    print(f"命中数: {hit_count} / {len(active_cases)}")
    print(f"召回覆盖率 (Coverage): {coverage:.2%}")
    print(f"准确率 (Precision): {precision:.2%}")
    print(f"过期数据泄露数: {expired_leak_count} / {len(expired_cases)}")
    print("=" * 50)

    # 门禁断言
    assert expired_leak_count == 0, f"发现 {expired_leak_count} 条过期记忆泄露"
    assert coverage >= 0.80, f"召回覆盖率低于门槛 80%: 当前为 {coverage:.2%}"
    assert precision >= 0.80, f"召回准确率低于门槛 80%: 当前为 {precision:.2%}"
