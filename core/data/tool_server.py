# -*- coding: utf-8 -*-
"""经营数据 HTTP 服务：鉴权、JSONL 调用日志、健康检查、schema 与只读查询路由。

Root Agent 使用上下文获取、任务初始化、经营查询、引用核验与证据回查端点。
保留接口与扩展路由按各自合同响应；mount_historical_surface 可挂载兼容路由面。
未挂载时相关路由返回结构化 503，不影响经营查询或健康检查。
默认调用日志位于 agents_oagent/evidence/runtime/，可由 TOOL_SERVER_LOG_PATH 覆盖。"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HISTORICAL_MODULES_OK = False
HISTORICAL_IMPORT_ERROR = ('历史路由面未挂载（独立启动；经兼容入口启动时'
                           '自动挂载扩展路由）')
TOOL_FUNCS = {}
SNAPSHOT_INDEX = {}
_historical_surface = None


def mount_historical_surface(surface):
    """挂载可选兼容路由处理器及其工具和快照索引。"""
    global _historical_surface, HISTORICAL_MODULES_OK, HISTORICAL_IMPORT_ERROR
    global TOOL_FUNCS, SNAPSHOT_INDEX
    _historical_surface = surface
    HISTORICAL_MODULES_OK = True
    HISTORICAL_IMPORT_ERROR = ''
    TOOL_FUNCS = surface.tool_funcs
    SNAPSHOT_INDEX = surface.snapshot_index


from . import pctx_query as _pctx  # noqa: E402
from . import qctx_period as _qperiod  # noqa: E402  # 仅取 PERIOD_CONTRACT_VERSION（pctx_v1 合同常量）
from . import oagent_ctx as _oagent  # noqa: E402


# 可选路由未挂载时返回结构化 503，经营查询和健康检查不受影响。
_HISTORICAL_ONLY_ROUTES = frozenset({
    'fulfillment_check', 'generate_historical', 'register_snapshot', 'worker_binding_info',
    'diagnosis_fixture', 'qctx_fixture',
    'qctx_query', 'qctx_condition', 'qctx_catalog', 'qctx_qev_guard',
})


def _historical_unavailable_payload():
    return {'ok': False, 'error_code': 'historical_unavailable',
            'detail': '兼容路由模块未装载：'
                      '本路由属历史-only 面，已显式降级；现行 oagent_*/pctx_* 路由不受影响',
            'import_error': HISTORICAL_IMPORT_ERROR,
            'demo_note': '可选路由模块未装载不阻止数据服务启动'}

# 周期查询独立登记 run 状态与 pqev 引用，不与快照或取证命名空间混用。
_PCTX_RUNS = {}
# 经营 Agent 第二层根任务登记表：按根 run_id 隔离的根任务上下文
# （允许周期/整体调用预算/两类证据登记）。与 _PCTX_RUNS 物理分离：
# 子工作流不得拿自己的 run_id 创建第二份经营查询状态（AI01_MAIN_LOOP.md）。
_OAGENT_RUNS = {}

def _pctx_run_state(run_id):
    rid = str(run_id or '').strip() or '__adhoc__'
    st = _PCTX_RUNS.get(rid)
    if st is None:
        st = _pctx.new_run_state()
        _PCTX_RUNS[rid] = st
    return st

PORT = 8099
LOG_PATH = os.environ.get('TOOL_SERVER_LOG_PATH') or os.path.join(
    _REPO, 'agents_oagent', 'evidence', 'runtime', 'tool_server_calls.jsonl')

SERVER_URL = 'http://host.docker.internal:%d' % PORT

API_KEY_ENV = 'DIAG_TOOLS_API_KEY'


def _api_key():
    """本地工具服务 Bearer 凭据"""
    val = os.environ.get(API_KEY_ENV, '').strip()
    if not val:
        raise RuntimeError('环境变量 %s 未设置：桥接凭据已不入库（2026-09-05 安全清理），'
                           '请在运行环境配置后重试' % API_KEY_ENV)
    return val

_log_lock = threading.Lock()

PCTX_OPENAPI_SCHEMA = {
    'openapi': '3.0.0',
    'info': {
        'title': 'AIGC Period Query Tools (pctx_v1, readonly)',
        'description': '周期入口只读服务面：执行 pctx_v1 周期查询（委托 qctx_period 唯一合同）、'
                       '核验周期证据引用（pqev: 命名空间，拒绝 qev:/ev: 跨命名空间）、'
                       '读取同源单期取证包（新视图对象/状态/窗口，不含旧 snapshot/Worker 身份）。'
                       '数据为 Demo 模拟口径。',
        'version': _pctx.PERIOD_ENTRY_CONTRACT_VERSION,
    },
    'paths': {
        '/pctx_query': {
            'post': {
                'summary': '执行一条 pctx_v1 周期查询（value/diff/set_compare/states/catalog）',
                'operationId': 'pctx_query',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'query'], 'properties': {
                        'run_id': {'type': 'string'},
                        'query': {'type': 'object', 'description': 'pctx_v1 请求对象（contract_version=pctx_v1 + request{op,...}）'},
                    }}}}},
                'responses': {'200': {'description': 'period_values/period_diff_set/period_set_compare/'
                                                     'period_object_states/period_catalog（附 query_id+qev）'
                                                     '| period_rejected(error_code)|budget_reached'}},
            },
        },
        '/pctx_qev_guard': {
            'post': {
                'summary': '周期证据引用动态守卫（pqev: 命名空间；拒绝跨命名空间引用）',
                'operationId': 'pctx_qev_guard',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'refs'], 'properties': {
                        'run_id': {'type': 'string'},
                        'refs': {'type': 'array', 'items': {'type': 'string'}},
                    }}}}},
                'responses': {'200': {'description': 'ok/accepted/rejected/foreign_namespace'}},
            },
        },
        '/pctx_evidence': {
            'post': {
                'summary': '同源单期取证包（新视图对象/状态/窗口一致校验；不读旧 fixture）',
                'operationId': 'pctx_evidence',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['product_id', 'period_id'], 'properties': {
                        'product_id': {'type': 'string'},
                        'period_id': {'type': 'string'},
                        'metrics': {'type': 'array', 'items': {'type': 'string'}},
                    }}}}},
                'responses': {'200': {'description': 'ok+evidence_id(pev:)+object/period/states/metric_values'
                                                     '| ok:false+error_code（unknown_object/namespace_conflict）'}},
            },
        },
    },
    'components': {'schemas': {}},
}

OPERATING_AGENT_OPENAPI_SCHEMA = {
    'openapi': '3.0.0',
    'info': {
        'title': 'AIGC Operating Agent Tools (oagent_root_v1, readonly)',
        'description': '经营 Agent 主 Agent 直接工具：根任务上下文初始化（幂等）、'
                       '根任务周期查询与单期取证（整体调用预算=6 次查询尝试+2 次取证+2 次专项'
                       '委派，失败/重复也扣减）、两类证据引用守卫（pqev 数值证据 ∈ 本根任务实际'
                       '查询返回；pev 取证 ∈ 本根任务实际取证登记；qev:/ev: 旧命名空间拒绝）、'
                       '专项经营调查（专项工作流：child_init 权威检查/证据解析、结果回传登记、'
                       '记录读数；本地合同路径 /oagent_specialist 为替身执行）。数据为 Demo 模拟口径。',
        'version': _oagent.OAGENT_CONTRACT_VERSION,
    },
    'paths': {
        '/oagent_ctx_fetch': {
            'post': {
                'summary': '无状态取入口上下文：商品目录（含 view_version）、周期信息、'
                           '入口选项投影（选择标签→id 映射按当前数据面现算）、shared_business_note '
                           '全文与版本组；不建状态、不计预算，run_id 仅作追踪',
                'operationId': 'oagent_ctx_fetch',
                'requestBody': {'required': False, 'content': {'application/json': {'schema': {
                    'type': 'object', 'properties': {
                        'run_id': {'type': 'string', 'description': '追踪用 run_id（可选，仅记录）'},
                    }}}}},
                'responses': {'200': {'description': 'ok+catalog+entry_projection+'
                                                     'shared_business_note+versions '
                                                     '（消费方失败即显式拒绝，禁止回退图内副本）'}},
            },
        },
        '/oagent_root_init': {
            'post': {
                'summary': '初始化/续用根任务上下文（幂等）：登记本轮允许周期（服务端周期守卫依据）；'
                           'expected_versions=取上下文响应 versions 原样回传，不一致=version_changed',
                'operationId': 'oagent_root_init',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'expected_versions'], 'properties': {
                        'run_id': {'type': 'string', 'description': '根任务 run_id（入口 sys.workflow_run_id，模型不可填）'},
                        'allowed_periods': {'type': 'array', 'items': {'type': 'string'},
                                            'description': '本轮允许周期（入口选择带入；重复 init 不得修改）'},
                        'allowed_product_ids': {'type': 'array', 'items': {'type': 'string'},
                                                'description': '本轮商品上界（仅入口显式选品类时带入；'
                                                               '缺省不设上界）'},
                        'expected_versions': {'type': 'object',
                                              'description': '取上下文响应 versions 原样回传；'
                                                             '缺失/不一致=version_changed 拒绝'},
                    }}}}},
                'responses': {'200': {'description': 'ok+event(root_created|root_exists)+allowed_periods+root_budget'
                                                     '| ok:false+error_code（root_run_id_missing/root_period_unknown/'
                                                     'root_period_mismatch/root_product_unknown/'
                                                     'root_product_duplicate/root_product_scope_mismatch/'
                                                     'version_changed）'}},
            },
        },
        '/oagent_query': {
            'post': {
                'summary': '根任务周期查询（主 Agent 直接工具；整体调用限制=每根任务 ≤6 次尝试）',
                'operationId': 'oagent_query',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'query'], 'properties': {
                        'run_id': {'type': 'string', 'description': '根任务 run_id（入口注入，模型不可填）'},
                        'query': {'type': 'object', 'description': 'pctx_v1 请求对象（request{op,...} 业务参数；'
                                                                   'contract_version 由服务端固定，周期须 ∈ 根任务允许周期）'},
                    }}}}},
                'responses': {'200': {'description': 'period_values/period_diff_set/period_set_compare/'
                                                     'period_object_states/period_catalog（附 query_id+qev+root_budget）'
                                                     '| period_rejected(error_code)|budget_reached|root_context_not_found'}},
            },
        },
        '/oagent_evidence': {
            'post': {
                'summary': '根任务单期取证（主 Agent 直接工具；实际响应登记进根任务证据注册表；≤2 次/根任务）',
                'operationId': 'oagent_evidence',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'product_id', 'period_id'], 'properties': {
                        'run_id': {'type': 'string', 'description': '根任务 run_id（入口注入，模型不可填）'},
                        'product_id': {'type': 'string'},
                        'period_id': {'type': 'string'},
                        'metrics': {'type': 'array', 'items': {'type': 'string'}},
                    }}}}},
                'responses': {'200': {'description': 'ok+evidence_id(pev:)+object/period/metric_values+source_check'
                                                     '| ok:false+error_code（root_context_not_found/'
                                                     'root_evidence_budget_reached/unknown_object/namespace_conflict）'}},
            },
        },
        '/oagent_qev_guard': {
            'post': {
                'summary': '根任务两类证据引用守卫（pqev 数值 ∈ 本根任务查询返回；pev 取证 ∈ 本根任务登记；'
                           'qev:/ev: 跨命名空间拒绝）',
                'operationId': 'oagent_qev_guard',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'refs'], 'properties': {
                        'run_id': {'type': 'string'},
                        'refs': {'type': 'array', 'items': {'type': 'string'}},
                    }}}}},
                'responses': {'200': {'description': 'ok/accepted/rejected/foreign_namespace+root_budget'}},
            },
        },
        #  最小只读出口：按 run_id + 引用回查本根任务**已登记**的原始查询/取证元素
        # （oagent_ctx.resolve_evidence_refs 同一实现；未签发/跨任务=missing，不发起任何新查询，
        # 不消耗预算）。仅服务层（:8327）证据详情投影消费，不注册成模型可见工具。
        '/oagent_evidence_resolve': {
            'post': {
                'summary': '按引用回查本根任务已登记的原始证据内容（只读；不发起查询、不扣预算）',
                'operationId': 'oagent_evidence_resolve',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'refs'], 'properties': {
                        'run_id': {'type': 'string', 'description': '根任务 run_id'},
                        'refs': {'type': 'array', 'items': {'type': 'string'},
                                 'description': 'canonical 引用（pqev:/pev:）；未登记引用逐项 missing'},
                    }}}}},
                'responses': {'200': {'description': 'ok+items[{ref,status,content,reason}]（content=根任务'
                                                     '登记响应原样元素）| ok:false+root_context_not_found'}},
            },
        },
        '/oagent_specialist': {
            'post': {
                'summary': '专项经营调查（本地合同路径；替身 runner，非真实 Qwen 调用）：'
                           'body {run_id, specialist_request}；run_id=根任务（父图注入，模型不填）',
                'operationId': 'oagent_specialist',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id', 'specialist_request'], 'properties': {
                        'run_id': {'type': 'string', 'description': '根任务 run_id（入口注入，模型不可填）'},
                        'specialist_request': {'type': 'object',
                                               'description': '专项委派请求：sub_question/target_objects/'
                                                              'benchmark_scope?/periods/evidence_refs?/'
                                                              'expected_delivery/user_question?'},
                    }}}}},
                'responses': {'200': {'description': 'ok+event(specialist_result)+findings/uncertainties/'
                                                     'completion/evidence_index/call_summary/root_budget'
                                                     '| ok:false+error_code（root_context_not_found/'
                                                     'specialist_budget_reached/request_not_json/unknown_keys/'
                                                     'missing_sub_question/unknown_object/period_out_of_scope/…）'}},
            },
        },
        '/oagent_child_init': {
            'post': {
                'summary': '专项上下文初始化：权威检查（根任务/请求/专项预算/'
                           '子执行 ID 绑定）+ 按引用解析真实证据内容',
                'operationId': 'oagent_child_init',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['parent_run_id', 'child_run_id',
                                                   'specialist_request', 'expected_versions'],
                    'properties': {
                        'parent_run_id': {'type': 'string', 'description': '父根任务 run_id'},
                        'child_run_id': {'type': 'string',
                                         'description': '子工作流自身 run_id（sys.workflow_run_id）；'
                                                        '只登记绑定，不另开查询状态'},
                        'specialist_request': {'type': 'object'},
                        'expected_versions': {'type': 'object',
                                              'description': '取上下文响应 versions 原样回传；'
                                                             '缺失/不一致=version_changed 拒绝'
                                                             '（在专项预算扣减前核对，不烧额度）'},
                    }}}}},
                'responses': {'200': {'description': 'ok+event(specialist_init)+evidence_resolved+'
                                                     'allowed_periods+data_version+root_budget'
                                                     '| ok:false+error_code（含 version_changed）'}},
            },
        },
        '/oagent_specialist_report': {
            'post': {
                'summary': '专项子工作流结果回传登记（子图 End 前调用）：结构化结果登记根任务，'
                           '发现引用逐条核验（机器证据记录=服务端登记，非模型自述）',
                'operationId': 'oagent_specialist_report',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['parent_run_id', 'child_run_id', 'result'],
                    'properties': {
                        'parent_run_id': {'type': 'string'},
                        'child_run_id': {'type': 'string'},
                        'result': {'type': 'object', 'description': 'findings/uncertainties/completion/'
                                                                    'completion_reason/raw_reply/…'},
                    }}}}},
                'responses': {'200': {'description': 'ok+event(specialist_report_registered)+'
                                                     'delegation_index+root_budget'
                                                     '| ok:false+error_code（child_run_id_not_bound_to_root/…）'}},
            },
        },
        '/oagent_specialist_status': {
            'post': {
                'summary': '专项委派记录读数（主图投影用；只读）',
                'operationId': 'oagent_specialist_status',
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['run_id'], 'properties': {
                        'run_id': {'type': 'string'},
                    }}}}},
                'responses': {'200': {'description': 'ok+records+root_budget'
                                                     '| ok:false+root_context_not_found'}},
            },
        },
    },
    'components': {'schemas': {}},
}

def _log_call(tool, snapshot_id, status, evidence_id=None, extra=None):
    entry = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'tool': tool,
        'snapshot_id': snapshot_id,
        'status': status,
        'evidence_id': evidence_id,
    }
    if extra:
        entry.update(extra)
    with _log_lock:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        auth = self.headers.get('Authorization', '')
        try:
            return auth == ('Bearer ' + _api_key())
        except RuntimeError:
            return False  # 凭据未配置：一律拒绝

    def do_GET(self):
        if self.path == '/openapi_pctx.json':
            self._send_json(200, PCTX_OPENAPI_SCHEMA)
        elif self.path == '/openapi_oagent.json':
            self._send_json(200, OPERATING_AGENT_OPENAPI_SCHEMA)
        elif self.path == '/healthz':
            self._send_json(200, {'ok': True, 'ts': time.strftime('%Y-%m-%d %H:%M:%S')})
        elif _historical_surface is not None:
            schema = _historical_surface.get_schema(self.path)
            if schema is not None:
                self._send_json(200, schema)
            else:
                self._send_json(404, {'error': 'not found'})
        else:
            self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        tool = self.path.strip('/')
        if not self._check_auth():
            self._send_json(401, {'error': 'invalid api key'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception:
            self._send_json(400, {'error': 'invalid json body'})
            return
        # —— 历史-only 路由降级闸：历史 (b) 侧模块不可用时显式 503，不进各分支 ——
        if tool in _HISTORICAL_ONLY_ROUTES and not HISTORICAL_MODULES_OK:
            _log_call(tool, '', '503_historical_unavailable')
            self._send_json(503, _historical_unavailable_payload())
            return
        # —— pctx_v1 周期入口只读加路：取数/公式/状态码全部委托 qctx_period，
        # 本层只加 run 级预算与 pqev: 引用登记（与 qctx_query/qev_guard 命名空间物理隔离）。
        # 语义：错误请求=200 period_rejected；预算超=200 budget_reached；未预期异常=500 tool_failure。
        if tool in ('pctx_query', 'pctx_evidence'):
            run_id = str(body.get('run_id') or '')
            if tool == 'pctx_evidence':
                try:
                    result = _pctx.single_period_evidence(
                        body.get('product_id'), body.get('period_id'),
                        body.get('metrics') or ('M1', 'M2'))
                except Exception as e:
                    _log_call(tool, run_id, '500_tool_failure', extra={'error': str(e)[:200]})
                    self._send_json(500, {'event': 'tool_failure', 'error': str(e)[:200]})
                    return
                _log_call(tool, run_id, '200_period_evidence',
                          extra={'ok': result.get('ok'), 'evidence_id': result.get('evidence_id')})
                self._send_json(200, result)
                return
            payload = body.get('query')
            if not isinstance(payload, dict):
                self._send_json(400, {'error': 'pctx_query 请求体缺失 query 对象'})
                return
            try:
                result = _pctx.execute_query(payload, run_state=_pctx_run_state(run_id))
            except Exception as e:  # 未预期异常=工具故障，不降级为 unknown
                _log_call(tool, run_id, '500_tool_failure', extra={'error': str(e)[:200]})
                self._send_json(500, {'event': 'tool_failure', 'error': str(e)[:200]})
                return
            _log_call(tool, run_id, '200_%s' % (result.get('event') or 'ok'),
                      extra={'error_code': result.get('error_code'),
                             'query_id': result.get('query_id')})
            self._send_json(200, result)
            return
        if tool == 'pctx_qev_guard':
            run_state = _pctx_run_state(body.get('run_id'))
            result = _pctx.validate_qev_refs(run_state, body.get('refs') or [])
            _log_call(tool, str(body.get('run_id') or ''), '200_period_qev_guard',
                      extra={'ok': result.get('ok'), 'rejected_n': len(result.get('rejected') or []),
                             'foreign_n': len(result.get('foreign_namespace') or [])})
            self._send_json(200, result)
            return
        # 上下文先无状态下发，再按可信 run_id 和 expected_versions 初始化根任务。
        # 任务丢失时明确拒绝，不静默重建预算。
        if tool == 'oagent_ctx_fetch':
            rid_seen = str(body.get('run_id') or '')
            result = _oagent.ctx_fetch(rid_seen)
            _log_call(tool, rid_seen or '(ctx)', '200_ctx_fetch',
                      extra={'ok': result.get('ok'), 'products_n':
                             len((result.get('catalog') or {}).get('products') or [])})
            self._send_json(200, result)
            return
        if tool == 'oagent_root_init':
            run_id = str(body.get('run_id') or '').strip()
            result = _oagent.init_root(run_id, body.get('allowed_periods'),
                                       registries=_OAGENT_RUNS,
                                       expected_versions=body.get('expected_versions'),
                                       allowed_product_ids=body.get('allowed_product_ids'))
            _log_call(tool, run_id, '200_%s' % result.get('event') if result.get('ok') else
                      '200_%s' % result.get('error_code'),
                      extra={'ok': result.get('ok'), 'periods_n': len(result.get('allowed_periods') or [])})
            self._send_json(200, result)
            return
        if tool == 'oagent_query':
            run_id = str(body.get('run_id') or '').strip()
            req = body.get('query')
            if not isinstance(req, dict):
                self._send_json(400, {'error': 'oagent_query 请求体缺失 query 对象'})
                return
            # 模型只填写业务请求；运行 ID 与协议版本由可信调用方和服务端控制。
            payload = {'contract_version': _qperiod.PERIOD_CONTRACT_VERSION,
                       'request': req}
            root_st = _OAGENT_RUNS.get(run_id)
            # root 不存在时不创建 pctx run_state（root_context_not_found 无副作用）
            pctx_rs = _pctx_run_state(run_id) if root_st is not None else None
            try:
                result = _oagent.execute_root_query(run_id, payload,
                                                    pctx_run_state=pctx_rs, root_state=root_st)
            except Exception as e:  # 未预期异常=工具故障，不降级为 unknown
                _log_call(tool, run_id, '500_tool_failure', extra={'error': str(e)[:200]})
                self._send_json(500, {'event': 'tool_failure', 'error': str(e)[:200]})
                return
            _log_call(tool, run_id, '200_%s' % (result.get('event') or 'ok'),
                      extra={'error_code': result.get('error_code'),
                             'query_id': result.get('query_id'),
                             'attempts': result.get('root_budget', {}).get('queries_attempted')})
            self._send_json(200, result)
            return
        if tool == 'oagent_evidence':
            run_id = str(body.get('run_id') or '').strip()
            try:
                result = _oagent.execute_root_evidence(
                    run_id, body.get('product_id'), body.get('period_id'),
                    body.get('metrics') or ('M1', 'M2'), root_state=_OAGENT_RUNS.get(run_id))
            except Exception as e:
                _log_call(tool, run_id, '500_tool_failure', extra={'error': str(e)[:200]})
                self._send_json(500, {'event': 'tool_failure', 'error': str(e)[:200]})
                return
            _log_call(tool, run_id, '200_%s' % ('ok' if result.get('ok') else result.get('error_code')),
                      extra={'ok': result.get('ok'), 'evidence_id': result.get('evidence_id')})
            self._send_json(200, result)
            return
        if tool == 'oagent_qev_guard':
            run_id = str(body.get('run_id') or '').strip()
            result = _oagent.validate_root_refs(run_id, body.get('refs') or [],
                                                root_state=_OAGENT_RUNS.get(run_id))
            _log_call(tool, run_id, '200_oagent_qev_guard',
                      extra={'ok': result.get('ok'), 'rejected_n': len(result.get('rejected') or []),
                             'foreign_n': len(result.get('foreign_namespace') or [])})
            self._send_json(200, result)
            return
        # ——  原始依据回查（只读；见 OpenAPI 段注释）——
        # 根任务不存在=200 结构化 root_context_not_found（items 空，不泄露任何原始数据）；
        # 未签发/跨任务引用由 resolve_evidence_refs 逐项 missing。本路径零调用查询/取证函数，
        # 预算与登记面零变化。
        if tool == 'oagent_evidence_resolve':
            run_id = str(body.get('run_id') or '').strip()
            refs = body.get('refs') or []
            if not isinstance(refs, list):
                self._send_json(400, {'error': 'oagent_evidence_resolve refs 必须是数组'})
                return
            st = _OAGENT_RUNS.get(run_id)
            if st is None:
                result = {'ok': False, 'event': 'oagent_evidence_resolve',
                          'error_code': 'root_context_not_found',
                          'detail': '根任务上下文不存在（run_id=%r）：不提供任何原始证据内容' % run_id,
                          'items': []}
            else:
                items = _oagent.resolve_evidence_refs(st, [str(r) for r in refs])
                result = {'ok': True, 'event': 'oagent_evidence_resolve',
                          'contract_version': _oagent.OAGENT_CONTRACT_VERSION,
                          'run_id': run_id, 'items': items,
                          'note': '只读回查本根任务登记元素；未签发/定位不到逐项 missing；'
                                  '本端点不发起查询或取证、不改变预算与登记面'}
            _log_call(tool, run_id, '200_oagent_evidence_resolve',
                      extra={'ok': result.get('ok'), 'refs_n': len(refs),
                             'resolved_n': sum(1 for it in result['items']
                                               if it.get('status') == 'resolved')})
            self._send_json(200, result)
            return
        # 专项端点分别负责上下文绑定、本地 runner 执行、结果登记与只读状态查询。
        if tool == 'oagent_specialist':
            run_id = str(body.get('run_id') or '').strip()
            root_st = _OAGENT_RUNS.get(run_id)
            pctx_rs = _pctx_run_state(run_id) if root_st is not None else None
            try:
                result = _oagent.execute_root_specialist(
                    run_id, body.get('specialist_request'), root_state=root_st,
                    pctx_run_state=pctx_rs)
            except Exception as e:  # 未预期异常=工具故障，不降级为 unknown
                _log_call(tool, run_id, '500_tool_failure', extra={'error': str(e)[:200]})
                self._send_json(500, {'event': 'tool_failure', 'error': str(e)[:200]})
                return
            _log_call(tool, run_id, '200_%s' % ('ok' if result.get('ok') else result.get('error_code')),
                      extra={'ok': result.get('ok'), 'completion': result.get('completion'),
                             'child_run_id': result.get('child_run_id'),
                             'specialist_calls': result.get('root_budget', {}).get('specialist_calls')})
            self._send_json(200, result)
            return
        if tool == 'oagent_child_init':
            parent = str(body.get('parent_run_id') or '').strip()
            child = str(body.get('child_run_id') or '').strip()
            try:
                result = _oagent.specialist_init(parent, child, body.get('specialist_request'),
                                                 root_state=_OAGENT_RUNS.get(parent),
                                                 expected_versions=body.get('expected_versions'))
            except Exception as e:
                _log_call(tool, parent, '500_tool_failure', extra={'error': str(e)[:200]})
                self._send_json(500, {'event': 'tool_failure', 'error': str(e)[:200]})
                return
            _log_call(tool, parent, '200_%s' % ('ok' if result.get('ok') else result.get('error_code')),
                      extra={'ok': result.get('ok'), 'child_run_id': child,
                             'resolved_n': len(result.get('evidence_resolved') or [])})
            self._send_json(200, result)
            return
        if tool == 'oagent_specialist_report':
            parent = str(body.get('parent_run_id') or '').strip()
            child = str(body.get('child_run_id') or '').strip()
            try:
                result = _oagent.specialist_report(parent, child, body.get('result'),
                                                   root_state=_OAGENT_RUNS.get(parent))
            except Exception as e:
                _log_call(tool, parent, '500_tool_failure', extra={'error': str(e)[:200]})
                self._send_json(500, {'event': 'tool_failure', 'error': str(e)[:200]})
                return
            _log_call(tool, parent, '200_%s' % ('ok' if result.get('ok') else result.get('error_code')),
                      extra={'ok': result.get('ok'), 'child_run_id': child,
                             'findings_n': len(result.get('findings') or [])})
            self._send_json(200, result)
            return
        if tool == 'oagent_specialist_status':
            run_id = str(body.get('run_id') or '').strip()
            result = _oagent.specialist_status(run_id, root_state=_OAGENT_RUNS.get(run_id))
            _log_call(tool, run_id, '200_oagent_specialist_status',
                      extra={'ok': result.get('ok'), 'records_n': len(result.get('records') or [])})
            self._send_json(200, result)
            return
        # 未匹配的路由交给已挂载的兼容处理器。
        if _historical_surface is not None:
            _historical_surface.handle_post(self, tool, body)
            return
        if not HISTORICAL_MODULES_OK:
            _log_call(tool, '', '503_historical_unavailable')
            self._send_json(503, _historical_unavailable_payload())
            return
        self._send_json(404, {'error': 'unknown tool: %s' % tool})

    def log_message(self, fmt, *args):
        pass  # 关闭默认 stderr 访问日志，避免 Windows 控制台编码问题

def main():
    _api_key()  # 启动即校验凭据已配置（缺失时明确失败，不回退默认值）
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print('tool data server listening on 0.0.0.0:%d (service URL: %s)' % (PORT, SERVER_URL), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
