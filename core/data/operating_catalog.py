# -*- coding: utf-8 -*-
"""模拟经营商品目录、周期、量级档位、轨迹与缺陷规则表。

包含 60 个商品与 8 个等长模拟周期；时间序由 period_index 表达。
经营指标、发布状态、缺失与冲突均为模拟数据；公开信息只用于部分商品身份锚点，
不代表真实平台经营结果。目录不包含评测答案，不读取快照或评测索引。
本模块仅维护常量与 import 期推导，无文件 IO 或模型调用。
period_view_generator 消费生成规则，qctx_period 消费周期与默认比较范围。"""

# ---- 8 周期（时间序只由 period_index 表达，ID 字符串不表达时间序）----
# 周期元组前两项是默认比较的基期和末期；其余周期不改变该默认值，时间排序另按 period_index。
PERIODS = (
    {'period_id': 'period_demo_p1', 'period_index': 6,
     'period_label': '上一周期（模拟）', 'period_start_date': '2026-05-14',
     'period_end_date': '2026-05-20', 'length_days': 7},
    {'period_id': 'period_demo_p2', 'period_index': 7,
     'period_label': '本周期（模拟）', 'period_start_date': '2026-05-21',
     'period_end_date': '2026-05-27', 'length_days': 7},
    {'period_id': 'period_demo_b1', 'period_index': 5,
     'period_label': '历史周期（模拟，回溯 1）', 'period_start_date': '2026-05-07',
     'period_end_date': '2026-05-13', 'length_days': 7},
    {'period_id': 'period_demo_b2', 'period_index': 4,
     'period_label': '历史周期（模拟，回溯 2）', 'period_start_date': '2026-04-30',
     'period_end_date': '2026-05-06', 'length_days': 7},
    {'period_id': 'period_demo_b3', 'period_index': 3,
     'period_label': '历史周期（模拟，回溯 3）', 'period_start_date': '2026-04-23',
     'period_end_date': '2026-04-29', 'length_days': 7},
    {'period_id': 'period_demo_b4', 'period_index': 2,
     'period_label': '历史周期（模拟，回溯 4）', 'period_start_date': '2026-04-16',
     'period_end_date': '2026-04-22', 'length_days': 7},
    {'period_id': 'period_demo_b5', 'period_index': 1,
     'period_label': '历史周期（模拟，回溯 5）', 'period_start_date': '2026-04-09',
     'period_end_date': '2026-04-15', 'length_days': 7},
    {'period_id': 'period_demo_b6', 'period_index': 0,
     'period_label': '历史周期（模拟，回溯 6）', 'period_start_date': '2026-04-02',
     'period_end_date': '2026-04-08', 'length_days': 7},
)
PERIOD_IDS = tuple(p['period_id'] for p in PERIODS)
PERIOD_INDEX = {p['period_id']: p['period_index'] for p in PERIODS}

_PRODUCTS = (
    {'product_id': '10000963504072', 'product_name': '【招牌推荐】脆皮全鸡', 'brand': '',
     'category': '西式快餐', 'city': '全国', 'price': 15.9, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '名称含营销词，不宣称来自任何公开页面；周期值/身份字段全 Demo'},
    {'product_id': '100114278550172', 'product_name': '全网低价【免配送费尖叫星期三】59 元披萨买一送一', 'brand': '',
     'category': '西式快餐', 'city': '华北', 'price': 59.0, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '"买一送一"是既有 Demo 结构，不宣称对应任何真实活动'},
    {'product_id': '10010408340172', 'product_name': '【紫禁新城】含小电锅双人餐 6 荤 1 素 1 主食', 'brand': '',
     'category': '中式快餐', 'city': '华南', 'price': 139.0, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '唯一既有 state_change 载体（p1 未发布→p2 已发布）'},
    {'product_id': '100114935800126', 'product_name': '【单杯起送】生椰抹茶麻薯兑换券 1 张', 'brand': '',
     'category': '奶茶饮品', 'city': '华中', 'price': 13.8, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '与 P37/P38 构成"生椰"前缀近名组'},
    {'product_id': '100009316510956', 'product_name': '【国庆特惠】奶皮子拿铁（含真实奶皮子）', 'brand': '',
     'category': '奶茶饮品', 'city': '华南', 'price': 12.9, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '掉量场景主载体'},
    {'product_id': '100115582301401', 'product_name': '【下午茶推荐】酥脆鸡排＋薯条单人套餐', 'brand': '',
     'category': '小吃', 'city': '华东', 'price': 19.9, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '用户 2026-09-16 给定', 'demo_fields_note': '—'},
    {'product_id': '100115582301402', 'product_name': '【街头小吃】爆汁烤肠双拼套餐', 'brand': '',
     'category': '小吃', 'city': '华中', 'price': 16.9, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）', 'demo_fields_note': '—'},
    {'product_id': '100115582301403', 'product_name': '【工作日午餐】招牌卤肉饭＋饮品套餐', 'brand': '',
     'category': '中式快餐', 'city': '华南', 'price': 29.9, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '与 P49 构成"卤肉饭"近名组'},
    {'product_id': '100115582301404', 'product_name': '【经典套餐】香辣鸡腿堡＋薯条＋可乐', 'brand': '',
     'category': '西式快餐', 'city': '华北', 'price': 25.9, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '与 P12 结构同名不同来源（三件套）'},
    {'product_id': '100115582301405', 'product_name': '【新品尝鲜】杨枝甘露大杯兑换券', 'brand': '',
     'category': '奶茶饮品', 'city': '全国', 'price': 15.9, 'anchor_form': '既有Demo目录',
     'src': [], 'public_fields_used': '无（本轮未做公开锚点核验）',
     'demo_fields_note': '与 P30 构成"杨枝甘露"近名组'},
    {'product_id': '100116600000011', 'product_name': '麦超所值·随心配 1+1（红区+白区）', 'brand': '麦当劳',
     'category': '西式快餐', 'city': '全国', 'price': 13.9, 'anchor_form': '具体价',
     'src': ('SRC-01',), 'public_fields_used': '品牌/名称/品类/价/结构/城市(含除外条款)',
     'demo_fields_note': '限时快照价（2025-01-01~01-14），不外推为常价'},
    {'product_id': '100116600000012', 'product_name': '大堡口福三件套（堡+中薯+中可乐）', 'brand': '麦当劳',
     'category': '西式快餐', 'city': '全国', 'price': 22.9, 'anchor_form': '具体价',
     'src': ('SRC-01',), 'public_fields_used': '品牌/名称/品类/价/结构/城市(含除外条款)',
     'demo_fields_note': '限时快照价（2025-01-01~01-14），不外推为常价'},
    {'product_id': '100116600000013', 'product_name': '小皇堡', 'brand': '汉堡王',
     'category': '西式快餐', 'city': '全国', 'price': 9.9, 'anchor_form': '具体价',
     'src': ('SRC-02',), 'public_fields_used': '品牌/名称/品类/价/结构',
     'demo_fields_note': '"周周 9.9"轮换款，品牌不固定'},
    {'product_id': '100116600000014', 'product_name': '香辣鸡腿中国汉堡单人套餐', 'brand': '塔斯汀',
     'category': '西式快餐', 'city': '全国', 'price': 9.9, 'anchor_form': '具体价',
     'src': ('SRC-02', 'SRC-03'), 'public_fields_used': '名称(官网)/品类(官网)/价(报道)',
     'demo_fields_note': '官网零标价 → 价格强度低于 P11'},
    {'product_id': '100116600000015', 'product_name': '蜜汁手扒鸡·脆皮全鸡＋可乐', 'brand': '华莱士',
     'category': '西式快餐', 'city': '全国', 'price': 19.9, 'anchor_form': '带内取值',
     'src': ('SRC-04', 'SRC-05', 'SRC-38'), 'public_fields_used': '名称(官网)/品类(官网)',
     'demo_fields_note': '价来自第三方代下单页（带 18.8–20.99）→ C 级'},
    {'product_id': '100116600000016', 'product_name': '8 英寸奥尔良烤鸡披萨', 'brand': '尊宝比萨',
     'category': '西式快餐', 'city': '全国', 'price': 22.5, 'anchor_form': '带内取值',
     'src': ('SRC-06',), 'public_fields_used': '名称/品类/规格/价格带（"不到 25 元"）',
     'demo_fields_note': '来源无精确数，取带中值'},
    {'product_id': '100116600000017', 'product_name': '双人餐（8 寸披萨+烤全鸡+两杯饮品）', 'brand': '尊宝比萨',
     'category': '西式快餐', 'city': '全国', 'price': 55.0, 'anchor_form': '具体价',
     'src': ('SRC-06',), 'public_fields_used': '名称/价/结构', 'demo_fields_note': '—'},
    {'product_id': '100116600000018', 'product_name': '爆香大粒鸡披萨', 'brand': '必胜客',
     'category': '西式快餐', 'city': '全国', 'price': 39.0, 'anchor_form': '具体价',
     'src': ('SRC-06',), 'public_fields_used': '名称/品类/价',
     'demo_fields_note': '限时限量（"39 元时代"）→ 新品期载体'},
    {'product_id': '100116600000019', 'product_name': '二人餐（个人披萨+小食）', 'brand': '乐凯撒',
     'category': '西式快餐', 'city': '华南', 'price': 99.9, 'anchor_form': '带内取值',
     'src': ('SRC-06',), 'public_fields_used': '名称/价格带（99.9–149 调后带）/结构',
     'demo_fields_note': '城市=Demo 指定'},
    {'product_id': '100116600000020', 'product_name': '原味大鸡排', 'brand': '正新鸡排',
     'category': '小吃', 'city': '全国', 'price': 11.4, 'anchor_form': '带内取值',
     'src': ('SRC-07',), 'public_fields_used': '品牌/名称/品类/价格带（9.9–13）',
     'demo_fields_note': '补贴软文口径；载体兼 X2 缺指标（全行 gpm=None）'},
    {'product_id': '100116600000021', 'product_name': '招牌鸭脖 250g', 'brand': '绝味鸭脖',
     'category': '小吃', 'city': '全国', 'price': 25.5, 'anchor_form': '带内取值',
     'src': ('SRC-08', 'SRC-09'), 'public_fields_used': '名称/规格/价格带（24–27）',
     'demo_fields_note': '按 250g 计价（单位陷阱）'},
    {'product_id': '100116600000022', 'product_name': '招牌鸭舌（小份约 80g）', 'brand': '绝味鸭脖',
     'category': '小吃', 'city': '全国', 'price': 30.0, 'anchor_form': '具体价',
     'src': ('SRC-09',), 'public_fields_used': '名称/规格/价',
     'demo_fields_note': '同页 250g 标 89 元 → 高价低频位'},
    {'product_id': '100116600000023', 'product_name': '锁鲜鸭脖 300g', 'brand': '周黑鸭',
     'category': '小吃', 'city': '全国', 'price': 34.5, 'anchor_form': '具体价',
     'src': ('SRC-10',), 'public_fields_used': '名称/规格/价（外卖折扣）',
     'demo_fields_note': '门店常价 42 → 载体兼 X3c 基期零（t2 全零记录）'},
    {'product_id': '100116600000024', 'product_name': '夫妻肺片 250g', 'brand': '紫燕百味鸡',
     'category': '小吃', 'city': '华北', 'price': 64.8, 'anchor_form': '具体价',
     'src': ('SRC-11',), 'public_fields_used': '名称/规格/价/城市',
     'demo_fields_note': '记者实付 44.58/0.172kg → 实付≠标价'},
    {'product_id': '100116600000025', 'product_name': '百味鸡+素拼+米饭单人餐', 'brand': '紫燕百味鸡',
     'category': '小吃', 'city': '华北', 'price': 24.0, 'anchor_form': '带内取值',
     'src': ('SRC-11',), 'public_fields_used': '名称/结构/价格带（20.8–27.4）/城市',
     'demo_fields_note': '来源含 2022→2023 涨价序列，不引入'},
    {'product_id': '100116600000026', 'product_name': '小郡肝 5 串', 'brand': '夸父炸串',
     'category': '小吃', 'city': '全国', 'price': 7.9, 'anchor_form': '具体价',
     'src': ('SRC-12',), 'public_fields_used': '名称/规格/价',
     'demo_fields_note': '载体兼 X3b 零分母（t5 全行 plc_ctr=None）'},
    {'product_id': '100116600000027', 'product_name': '炸排骨 5 串', 'brand': '喜姐炸串',
     'category': '小吃', 'city': '华东', 'price': 11.0, 'anchor_form': '带内取值',
     'src': ('SRC-13',), 'public_fields_used': '名称/规格/价（带 6–11）',
     'demo_fields_note': '载体兼 X3a 零分母（t3 全行 vv=0）'},
    {'product_id': '100116600000028', 'product_name': '乐事薯片 35g（量贩渠道）', 'brand': '好想来',
     'category': '小吃', 'city': '华中', 'price': 2.8, 'anchor_form': '具体价',
     'src': ('SRC-39',), 'public_fields_used': '名称/规格/价',
     'demo_fields_note': '商超 40g/3 元 → 规格不可混用；载体兼 X1 缺周期（t0 无记录）'},
    {'product_id': '100116600000029', 'product_name': '冰鲜柠檬水', 'brand': '蜜雪冰城',
     'category': '奶茶饮品', 'city': '全国', 'price': 4.0, 'anchor_form': '具体价',
     'src': ('SRC-14',), 'public_fields_used': '品牌/名称/价',
     'demo_fields_note': '全目录最低价 → 低价高动销位'},
    {'product_id': '100116600000030', 'product_name': '杨枝甘露', 'brand': '茶百道',
     'category': '奶茶饮品', 'city': '全国', 'price': 17.0, 'anchor_form': '具体价',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类/价（cha-bai-dao 页已复核）',
     'demo_fields_note': '与 P10 近名'},
    {'product_id': '100116600000031', 'product_name': '超 A 芝士葡萄', 'brand': '古茗',
     'category': '奶茶饮品', 'city': '华东', 'price': 19.0, 'anchor_form': '具体价',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类/价（gu-ming 页）',
     'demo_fields_note': '城市=Demo 指定；载体兼 X2 缺指标（部分行 plc_ctr=None）'},
    {'product_id': '100116600000032', 'product_name': '多肉葡萄', 'brand': '喜茶',
     'category': '奶茶饮品', 'city': '全国', 'price': 19.0, 'anchor_form': '带内取值',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类/价格带（18–19）',
     'demo_fields_note': '"首创"字样不进名称'},
    {'product_id': '100116600000033', 'product_name': '奶茶三兄弟', 'brand': 'CoCo 都可',
     'category': '奶茶饮品', 'city': '全国', 'price': 12.0, 'anchor_form': '具体价',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类/价（coco-dou-ke 页）', 'demo_fields_note': '—'},
    {'product_id': '100116600000034', 'product_name': '益禾烤奶', 'brand': '益禾堂',
     'category': '奶茶饮品', 'city': '华中', 'price': 8.0, 'anchor_form': '具体价',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类/价（yi-he-tang 页）',
     'demo_fields_note': '城市=Demo 指定；载体兼 X2 缺指标（部分行 vv=None）'},
    {'product_id': '100116600000035', 'product_name': '书亦烧仙草', 'brand': '书亦烧仙草',
     'category': '奶茶饮品', 'city': '华南', 'price': 10.0, 'anchor_form': '具体价',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类/价（shu-yi-shao-xian-cao 页）',
     'demo_fields_note': '品牌名=商品名，单靠名称不可消歧'},
    {'product_id': '100116600000036', 'product_name': '拿铁咖啡（237–355ml）', 'brand': 'Manner',
     'category': '奶茶饮品', 'city': '华东', 'price': 15.0, 'anchor_form': '具体价',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类/价/规格（manner-coffee 页）',
     'demo_fields_note': '载体兼 X1 缺周期（t2 无记录）+ X4 record conflict（t1 同键双记录）'},
    {'product_id': '100116600000037', 'product_name': '生椰拿铁（首创）', 'brand': '瑞幸咖啡',
     'category': '奶茶饮品', 'city': '全国', 'price': 13.9, 'anchor_form': '带内取值',
     'src': ('SRC-15',), 'public_fields_used': '名称/品类（rui-xing-ka-fei 页；到手 9.9–13.9，标价 32）',
     'demo_fields_note': '同品双价：price 取到手带上限，标价 32 只作证据不进运行时'},
    {'product_id': '100116600000038', 'product_name': '生椰拿铁', 'brand': '库迪咖啡',
     'category': '奶茶饮品', 'city': '全国', 'price': 27.0, 'anchor_form': '带内取值',
     'src': ('SRC-16',), 'public_fields_used': '名称/品类/价格带（零售 26–35；特价 9.9）',
     'demo_fields_note': '与 P37 同名不同品牌；price 取零售带，与 P37 口径相反 → 有意的价格口径对照'},
    {'product_id': '100116600000039', 'product_name': '正宗葡式蛋挞', 'brand': '味多美',
     'category': '烘焙甜品', 'city': '华北', 'price': 6.5, 'anchor_form': '具体价',
     'src': ('SRC-17',), 'public_fields_used': '品牌/名称/价/规格/城市（官方商城已复核）',
     'demo_fields_note': '全目录最强官方价'},
    {'product_id': '100116600000040', 'product_name': '毛毛虫面包 90g', 'brand': '味多美',
     'category': '烘焙甜品', 'city': '华北', 'price': 12.9, 'anchor_form': '具体价',
     'src': ('SRC-18',), 'public_fields_used': '名称/价/规格/城市（官方商城）',
     'demo_fields_note': '载体兼 X1 缺周期（t0、t1 无记录）'},
    {'product_id': '100116600000041', 'product_name': '黑芝麻芝士盐面包', 'brand': '鲍师傅糕点',
     'category': '烘焙甜品', 'city': '华中', 'price': 5.5, 'anchor_form': '具体价',
     'src': ('SRC-20',), 'public_fields_used': '名称/价/城市（武汉门店实探）', 'demo_fields_note': '—'},
    {'product_id': '100116600000042', 'product_name': '肉松小贝 4 个装', 'brand': '鲍师傅糕点',
     'category': '烘焙甜品', 'city': '全国', 'price': 19.0, 'anchor_form': '具体价',
     'src': ('SRC-19',), 'public_fields_used': '名称/规格/价（报道"约 19"）',
     'demo_fields_note': '报道口径 → 强度低于 P41'},
    {'product_id': '100116600000043', 'product_name': '抹茶吐司', 'brand': '鲍师傅糕点',
     'category': '烘焙甜品', 'city': '华中', 'price': 14.0, 'anchor_form': '具体价',
     'src': ('SRC-20',), 'public_fields_used': '名称/价/城市',
     'demo_fields_note': '与 P44 同名不同品牌，价差 12 元 → 横向对照'},
    {'product_id': '100116600000044', 'product_name': '抹茶吐司', 'brand': 'UH 祐禾',
     'category': '烘焙甜品', 'city': '华中', 'price': 26.0, 'anchor_form': '具体价',
     'src': ('SRC-20',), 'public_fields_used': '名称/价/城市',
     'demo_fields_note': '与 P43 同名不同品牌；G7 载体'},
    {'product_id': '100116600000045', 'product_name': '半熟芝士（原味 5 枚 180g）', 'brand': '好利来',
     'category': '烘焙甜品', 'city': '全国', 'price': 42.0, 'anchor_form': '带内取值',
     'src': ('SRC-21',), 'public_fields_used': '名称/规格/价格带（39–45）',
     'demo_fields_note': '载体兼 X2 缺指标（全行 paid_orders=None）；券面值 200 未采用'},
    {'product_id': '100116600000046', 'product_name': '雪月饼礼盒', 'brand': '元祖',
     'category': '烘焙甜品', 'city': '华东', 'price': 220.0, 'anchor_form': '带内取值',
     'src': ('SRC-22',), 'public_fields_used': '名称/价格带（"200 多元"）/城市',
     'demo_fields_note': '节令商品 → G6 掉量的现实解释面'},
    {'product_id': '100116600000047', 'product_name': '鲜炒区川味小炒肉', 'brand': '乡村基',
     'category': '中式快餐', 'city': '华东', 'price': 36.0, 'anchor_form': '具体价',
     'src': ('SRC-23',), 'public_fields_used': '名称/规格/价/城市（同页自选区 18 元）',
     'demo_fields_note': '同品名两档价（自选 vs 鲜炒）'},
    {'product_id': '100116600000048', 'product_name': '冬菇鸡竹丝鸡汤简餐', 'brand': '真功夫',
     'category': '中式快餐', 'city': '全国', 'price': 21.8, 'anchor_form': '具体价',
     'src': ('SRC-24',), 'public_fields_used': '名称/价（活动价；原价 26.8）',
     'demo_fields_note': '活动价，"54 天新低"'},
    {'product_id': '100116600000049', 'product_name': '卤肉饭随心选三件套', 'brand': '永和大王',
     'category': '中式快餐', 'city': '全国', 'price': 45.0, 'anchor_form': '带内取值',
     'src': ('SRC-25',), 'public_fields_used': '名称/结构/价格带（42–48）',
     'demo_fields_note': '与 P08 近名；周末五折后"20 多块"不进锚点；载体兼 X2（t3 单行多字段 None）'},
    {'product_id': '100116600000050', 'product_name': '招牌石锅拌饭', 'brand': '米村拌饭',
     'category': '中式快餐', 'city': '华北', 'price': 25.0, 'anchor_form': '带内取值',
     'src': ('SRC-26',), 'public_fields_used': '名称/价格带（20–30）/结构',
     'demo_fields_note': '客单≈32 作第 2 组档位依据'},
    {'product_id': '100116600000051', 'product_name': '自选套餐（3 荤 4 素+主食）', 'brand': '杨国福麻辣烫',
     'category': '中式快餐', 'city': '全国', 'price': 45.0, 'anchor_form': '带内取值',
     'src': ('SRC-27',), 'public_fields_used': '名称/结构/价格带（32.46–58.46）',
     'demo_fields_note': '2023-06 旧数据 + 加盟自定价 → 只作带'},
    {'product_id': '100116600000052', 'product_name': '自选套餐（3 荤 4 素+主食）', 'brand': '张亮麻辣烫',
     'category': '中式快餐', 'city': '全国', 'price': 45.0, 'anchor_form': '带内取值',
     'src': ('SRC-27',), 'public_fields_used': '名称/结构/价格带（36.38–54.38）',
     'demo_fields_note': '与 P51 名称完全相同（只能靠 brand/ID 定位）；载体兼 X1 缺周期（t6=p1 无记录）+ X7 停更复更'},
    {'product_id': '100116600000053', 'product_name': '宝宝番茄面套餐', 'brand': '和府捞面',
     'category': '中式快餐', 'city': '全国', 'price': 26.0, 'anchor_form': '具体价',
     'src': ('SRC-28',), 'public_fields_used': '名称/价/结构（已复核）',
     'demo_fields_note': '品牌带 25–108 元 → 只取被复核的套餐价'},
    {'product_id': '100116600000054', 'product_name': '"六荤三素"工作日单人餐', 'brand': '海底捞',
     'category': '火锅烧烤', 'city': '全国', 'price': 45.9, 'anchor_form': '带内取值',
     'src': ('SRC-29',), 'public_fields_used': '名称/结构/价格带（24.72–45.9）',
     'demo_fields_note': '取带上限；同页主品牌人均 97.9 属第 2 组；载体兼 X4 record conflict（t6=p1 同键双记录）'},
    {'product_id': '100116600000055', 'product_name': '火锅影厅双人套餐（含 2 票+礼盒）', 'brand': '海底捞',
     'category': '火锅烧烤', 'city': '全国', 'price': 398.0, 'anchor_form': '具体价',
     'src': ('SRC-30',), 'public_fields_used': '名称/价/结构',
     'demo_fields_note': '含非餐饮实物，全目录最高价；载体兼 X5 身份不一致（t3 起名称漂移）'},
    {'product_id': '100116600000056', 'product_name': '50 元以下单人小火锅套餐', 'brand': '呷哺呷哺',
     'category': '火锅烧烤', 'city': '华北', 'price': 50.0, 'anchor_form': '带内取值',
     'src': ('SRC-31',), 'public_fields_used': '名称/价格带（20–50）',
     'demo_fields_note': '官方菜单价未公开 → 带上限'},
    {'product_id': '100116600000057', 'product_name': '招牌毛肚+锅底组合', 'brand': '巴奴毛肚火锅',
     'category': '火锅烧烤', 'city': '全国', 'price': 168.0, 'anchor_form': 'Demo synthetic',
     'src': ('SRC-32',), 'public_fields_used': '名称/品类/结构（价格不属公开锚点）',
     'demo_fields_note': '无公开单品/套餐售价（来源只有人均 138–150，人均只用于量级档位）；'
                        'price 为 Demo 拟定值，刻意不等于公开人均；载体兼 X1 缺周期（t4 无记录）'},
    {'product_id': '100116600000058', 'product_name': '双人工作日午市套餐', 'brand': '凑凑火锅',
     'category': '火锅烧烤', 'city': '华东', 'price': 188.0, 'anchor_form': '具体价',
     'src': ('SRC-33',), 'public_fields_used': '名称/价/城市',
     'demo_fields_note': '套餐内菜品明细来源未列 → 不补造结构'},
    {'product_id': '100116600000059', 'product_name': '锅底+鲜牛肉引流组合', 'brand': '楠火锅',
     'category': '火锅烧烤', 'city': '华中', 'price': 39.9, 'anchor_form': '带内取值',
     'src': ('SRC-34',), 'public_fields_used': '名称/结构/价（创始人目标价）',
     'demo_fields_note': '目标价≠成交价的口径陷阱；城市=Demo 指定'},
    {'product_id': '100116600000060', 'product_name': '烤羔羊肉串（多人餐）', 'brand': '木屋烧烤',
     'category': '火锅烧烤', 'city': '华南', 'price': 128.0, 'anchor_form': 'Demo synthetic',
     'src': ('SRC-35',), 'public_fields_used': '品类/城市范围（价格与单品名不属公开锚点）',
     'demo_fields_note': '来源为 2021 年券商口径人均 93，未公布单品串价 → 人均只用于量级档位；'
                        'price=Demo 拟定多人餐值，不等于人均；载体兼 X2 缺指标（全行 vv=None）'},
)

PRODUCTS = tuple(_PRODUCTS)
PRODUCT_IDS = tuple(p['product_id'] for p in PRODUCTS)
LEGACY_PRODUCT_IDS = tuple(p['product_id'] for p in PRODUCTS[:10])

# 量级与转化档位决定行数、播放区间和转化率；参数为缩放前值。
MAGNITUDE_TIERS = {
    'LOW': {'rows': 2, 'vv_range': (800, 1500), 'cvr': 0.12},
    'MID': {'rows': 4, 'vv_range': (1200, 3500), 'cvr': 0.12},
    'HIGH': {'rows': 6, 'vv_range': (6000, 15000), 'cvr': 0.12},
    # MID_LEGACY 用于 P01–P10 的 t0..t5；t6/t7 直接使用生成器 SIM_PARAMS。
    'MID_LEGACY': {'rows': 4, 'vv_range': (300, 2500), 'cvr': 0.12},
}

_TIER_ASSIGNMENT = {
    '10000963504072': 'MID', '100114278550172': 'MID', '10010408340172': 'MID',
    '100114935800126': 'MID', '100009316510956': 'MID',
    '100115582301401': 'MID', '100115582301402': 'MID', '100115582301403': 'MID',
    '100115582301404': 'MID', '100115582301405': 'MID',
    # HIGH（15）
    '100116600000012': 'HIGH', '100116600000018': 'HIGH', '100116600000026': 'HIGH',
    '100116600000027': 'HIGH', '100116600000028': 'HIGH', '100116600000029': 'HIGH',
    '100116600000030': 'HIGH', '100116600000032': 'HIGH', '100116600000034': 'HIGH',
    '100116600000035': 'HIGH', '100116600000039': 'HIGH', '100116600000041': 'HIGH',
    '100116600000050': 'HIGH', '100116600000054': 'HIGH', '100116600000059': 'HIGH',
    # LOW（13）
    '100116600000019': 'LOW', '100116600000020': 'LOW', '100116600000022': 'LOW',
    '100116600000024': 'LOW', '100116600000025': 'LOW', '100116600000031': 'LOW',
    '100116600000044': 'LOW', '100116600000045': 'LOW', '100116600000046': 'LOW',
    '100116600000055': 'LOW', '100116600000057': 'LOW', '100116600000058': 'LOW',
    '100116600000060': 'LOW',
    # MID 新增（22）
    '100116600000011': 'MID', '100116600000013': 'MID', '100116600000014': 'MID',
    '100116600000015': 'MID', '100116600000016': 'MID', '100116600000017': 'MID',
    '100116600000021': 'MID', '100116600000023': 'MID', '100116600000033': 'MID',
    '100116600000036': 'MID', '100116600000037': 'MID', '100116600000038': 'MID',
    '100116600000040': 'MID', '100116600000042': 'MID', '100116600000043': 'MID',
    '100116600000047': 'MID', '100116600000048': 'MID', '100116600000049': 'MID',
    '100116600000051': 'MID', '100116600000052': 'MID', '100116600000053': 'MID',
    '100116600000056': 'MID',
}
TIER = {pid: ('MID_LEGACY' if pid in LEGACY_PRODUCT_IDS else _TIER_ASSIGNMENT[pid])
        for pid in PRODUCT_IDS}

# G7/G8 两轴同设参数（≈ctr×cvr×1000，单轴不可达 → 两轴同设才严格分离）
G7_CTR_RANGE = (0.035, 0.045)   # LOW 档 + ctr 上带 + cvr 0.24 → 带 8.4–10.8（高于其余全部上界 5.4）
G7_CVR = 0.24
G8_CTR_RANGE = (0.012, 0.018)   # HIGH 档 + ctr 下带 + cvr 0.04 → 带 0.48–0.72（低于普通载体下界 1.44）
G8_CVR = 0.04

# 轨迹由周期锚点与噪声生成；P01–P10 的轨迹模板仅作用于 t0..t5。
_TEMPLATE_MAIN = {
    # G1 稳定增长（8；既有 P08）
    '100115582301403': 'G1', '100116600000011': 'G1', '100116600000013': 'G1',
    '100116600000029': 'G1', '100116600000034': 'G1', '100116600000039': 'G1',
    '100116600000047': 'G1', '100116600000056': 'G1',
    # G2 稳定下滑（9；既有 P02/P10）
    '100114278550172': 'G2', '100115582301405': 'G2', '100116600000022': 'G2',
    '100116600000024': 'G2', '100116600000032': 'G2', '100116600000042': 'G2',
    '100116600000045': 'G2', '100116600000058': 'G2', '100116600000060': 'G2',
    # G3 先涨后跌（7；既有 P07）
    '100115582301402': 'G3', '100116600000014': 'G3', '100116600000026': 'G3',
    '100116600000030': 'G3', '100116600000043': 'G3', '100116600000050': 'G3',
    '100116600000053': 'G3',
    # G4 冷启动后放量（7；既有 P01）
    '10000963504072': 'G4', '100116600000016': 'G4', '100116600000028': 'G4',
    '100116600000036': 'G4', '100116600000041': 'G4', '100116600000049': 'G4',
    '100116600000037': 'G4',
    # G5 持续动销与平稳期。
    '10010408340172': 'G5', '100115582301401': 'G5', '100116600000015': 'G5',
    '100116600000021': 'G5', '100116600000033': 'G5', '100116600000038': 'G5',
    '100116600000048': 'G5', '100116600000051': 'G5', '100116600000027': 'G5',
    '100116600000040': 'G5',
    # G6 历史有动销后掉量（6；既有 P05）
    '100009316510956': 'G6', '100116600000017': 'G6', '100116600000023': 'G6',
    '100116600000046': 'G6', '100116600000052': 'G6', '100116600000055': 'G6',
    # G7 低流量高转化。
    '100114935800126': 'G7', '100115582301404': 'G7',
    '100116600000019': 'G7', '100116600000025': 'G7', '100116600000031': 'G7',
    '100116600000044': 'G7', '100116600000057': 'G7', '100116600000020': 'G7',
    # G8 高流量低转化（5；P18 同时叠加 G9）
    '100116600000012': 'G8', '100116600000018': 'G8', '100116600000035': 'G8',
    '100116600000054': 'G8', '100116600000059': 'G8',
}
# G9 叠加（HIGH 档 + 下滑形状 → 末期绝对水平前列但变化率垫底）
_TEMPLATE_OVERLAY_G9 = ('100116600000018', '100116600000032', '100116600000054')

# t7 播放量锚点按档位分带；MID_LEGACY 与 P01–P10 的 t6/t7 保持同量级，避免时间衔接突跳。
_T7_BAND = {'LOW': (160000.0, 300000.0), 'MID': (480000.0, 1400000.0),
            'HIGH': (3600000.0, 9000000.0), 'MID_LEGACY': (286000.0, 720000.0)}


def t7_anchor_m2(tier):
    """t7 锚点 （缩放后）：分带内固定随机取点（种子=档名，确定性，不依赖运行 seed）。"""
    import random as _r
    lo, hi = _T7_BAND[tier]
    return int(_r.Random('sc02-t7-anchor-' + tier).uniform(lo, hi))


# 各模板的周期系数控制增长、下滑、峰值、冷启动和平稳等模拟走势。
# G6 的前段锚点额外提高，形成曾有动销后明显掉量的形态。
_SHAPE = {
    'G1': (0.50, 0.60, 0.72, 0.84, 0.92, 0.96, 0.98, 1.00),
    'G2': (1.90, 1.70, 1.50, 1.35, 1.20, 1.10, 1.05, 1.00),
    'G3': (0.60, 1.30, 1.90, 1.60, 1.30, 1.10, 1.05, 1.00),          # 峰值 t2
    'G4': (0.02, 0.03, 0.04, 0.50, 0.80, 0.92, 0.97, 1.00),          # t0..t2 近零，t3 起放量
    'G5': (0.98, 1.02, 0.97, 1.03, 0.99, 1.01, 1.00, 1.00),          # 平台期（≤±3%）
    'G6': (1.00, 0.97, 0.94, 0.91, 0.50, 0.30, 0.14, 0.16),          # 前段高位、末段塌（t6/t7 低）
    'G7': (1.00, 0.98, 1.02, 0.97, 1.03, 0.99, 1.00, 1.00),
    'G8': (1.00, 1.02, 0.98, 1.01, 0.99, 1.00, 0.98, 1.00),
}
# G6 前段（t0..t3）锚点 = t7 锚 × 本系数（把前段提到带内上限，末段塌才够 3× 判据）
_G6_T0_ANCHOR_RATIO = 3.2
# G9 不单独设置轨迹，通过高量级档位叠加下滑或平稳形状形成高水平、低增速组合。

# ---- 缺陷载体（X1–X6 只落 P11–P60；X7 含既有 P03）----
# X1 缺周期：该 (商品, period_index) 不出记录
X1_MISSING_PERIODS = {
    '100116600000028': (0,),       # P28 缺 t0
    '100116600000040': (0, 1),     # P40 缺 t0,t1
    '100116600000052': (6,),       # P52 缺 t6=p1（p1→p2 差值不可给）
    '100116600000057': (4,),       # P57 缺 t4
    '100116600000036': (2,),       # P36 缺 t2
}
# X2 缺指标：行字段置 None（不得用 0 代替）。值=(字段, 范围)：'all'=全行；(lo,hi)=行号闭区间
X2_MISSING_FIELDS = {
    '100116600000020': (('gpm', 'all'),),                    # P20 全行 gpm=None（field_all_missing）
    '100116600000031': (('plc_ctr', (0, 1)),),               # P31 部分行 plc_ctr=None
    '100116600000034': (('vv', (1, 2)),),                    # P34 部分行 vv=None
    '100116600000045': (('paid_orders', 'all'),),            # P45 全行 paid_orders=None
    '100116600000049': (('gpm', (2, 2)), ('plc_ctr', (2, 2)), ('vv', (2, 2))),  # P49 t3 单行多字段
    '100116600000060': (('vv', 'all'), ('gpm', (0, 2))),     # P60 全行 vv=None + 部分行 gpm=None
}
# X3 零分母（记录级；实现见生成器注入层）
X3_ZERO_DENOMINATOR = {
    '100116600000027': {'period_index': 3, 'kind': 'all_vv_zero'},        # X3a：t3 全行 vv=0（/零分母）
    '100116600000026': {'period_index': 5, 'kind': 'all_plc_ctr_missing'},  # X3b：t5 全行 plc_ctr=None（零分母）
    '100116600000023': {'period_index': 2, 'kind': 'all_zero_record'},     # X3c：t2 记录存在但绝对量全 0（基期 0）
}
# X4 record conflict：同 (商品,period_index) 两条 _record_fingerprint 不同的记录
# （冲突副本=同记录改 period_orders，指纹含 period_orders → 两条互异；查询侧整体 record_conflict，不择一不平均）
X4_RECORD_CONFLICT = {
    '100116600000054': {'period_index': 6},   # P54 @ t6=p1
    '100116600000036': {'period_index': 1},   # P36 @ t1
}
# X5 identity inconsistency：同一 product_id 跨期 product_name 两个值
# （t0..t2 用名称 A，t3 起用名称 B；objects 目录名=B 为主名）
X5_IDENTITY_DRIFT = {
    '100116600000055': {'period_index': 3, 'alt_name': '火锅影厅双人套餐（含 2 票+礼盒+爆米花）'},
}
# X6 attribute drift：跨期 city 不同（t0..t2 用 alt_city，t3 起用目录 city）
X6_ATTRIBUTE_DRIFT = {
    '100116600000019': {'period_index': 3, 'field': 'city', 'alt_value': '全国'},
}

# ---- 发布状态规则表（X7；既有 P01–P10 两期值不动，P03 保持唯一既有 false→true 样例）----
# 未列出的新增商品默认 8 期全 True。published=False 的期 days_after_publish 落 15–21（沿用既有合同）。
PUBLISHED_SCHEDULE = {
    # P01–P10 的回溯期沿用 p1 发布状态，保持商品发布节奏。
    '10000963504072': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100114278550172': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '10010408340172': {0: False, 1: False, 2: False, 3: False, 4: False, 5: False, 6: False, 7: True},
    '100114935800126': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100009316510956': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100115582301401': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100115582301402': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100115582301403': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100115582301404': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100115582301405': {0: True, 1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    # X7 新增载体（P16 停更 2 期后复更 / P28 中期停更 / P36、P41、P52 跨期状态变化）
    '100116600000016': {0: True, 1: True, 2: False, 3: False, 4: True, 5: True, 6: True, 7: True},
    '100116600000028': {0: True, 1: True, 2: False, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100116600000036': {0: False, 1: False, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100116600000041': {0: True, 1: False, 2: False, 3: True, 4: True, 5: True, 6: True, 7: True},
    '100116600000052': {0: True, 1: True, 2: True, 3: True, 4: False, 5: False, 6: True, 7: True},
}


def published_flag(product_id, period_index):
    """该 (商品, 周期) 的 published 值（规则表查不到=默认 True，）。"""
    sched = PUBLISHED_SCHEDULE.get(str(product_id))
    if sched is None:
        return True
    return bool(sched.get(int(period_index), True))


def product_entry(product_id):
    for p in PRODUCTS:
        if p['product_id'] == str(product_id):
            return p
    return None


DATA_COVERAGE_START = '2026-03-01'
DATA_COVERAGE_END = '2026-05-27'
DEMO_BUSINESS_DATE = '2026-05-28'
LATEST_COMPLETE_DATE = '2026-05-27'

# 日级供给/转化形状（**不改变任何周记录数值**，只决定“周总量 → 7 天”的分布）。
# 索引＝星期（0＝周一 … 6＝周日）；取值是相对水平，实际使用时按周（或按日）归一，
# 因此“把周总量除以 7”不是本分布的一部分：同一天不同商品、同一商品不同周的形状都不同。
DAILY_SUPPLY_WEEKDAY = (0.92, 0.95, 0.98, 1.02, 1.12, 1.18, 1.10)      # 内容供给/流量：周末走高
DAILY_CONVERSION_WEEKDAY = (0.90, 0.94, 1.00, 1.06, 1.12, 1.14, 1.02)  # 成交转化：工作日午高峰
DAILY_TREND = (0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.06)               # 周内轻微上升趋势
# 新增模拟日（3 月 + 4 月 1 日）相对该商品**最早一期周水平**的水平：整体略低（供“完整月对比”
# 读到变化），逐日再叠加轻微上行，使 3 月内部也有可解释起伏（不写成真实同比/环比）。
MARCH_BASE_LEVEL = 0.95
MARCH_DAILY_TREND = (0.96, 1.02)     # 3 月内部自月初到月末的线性水平带（起、止）