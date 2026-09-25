import os
import time
import json
import atexit
import signal
import threading
from controllers.oauth2 import (
    get_oauth2_token, CLIENT_ID, _extract_code_from_url,
    build_auth_url, _wait_for_auth_entry_state,
    _perform_login_after_cookie_fail, _click_consent_and_exchange, _exchange_captured_code,
    _settle_auth_page, _disable_auth_page_autofill, AUTH_NAV_TIMEOUT_MS, AUTH_ENTRY_TIMEOUT_MS,
    _resolve_account_type, _dump_auth_page, configure_oauth2,
)
from concurrent.futures import ThreadPoolExecutor
from utils import random_email, generate_strong_password
from controllers.outlook_controller import OutlookController

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Results')  # 输出目录（oauth2.txt在此）
RESULT_WRITE_LOCK = threading.Lock()
# 默认 fingerprint 目录（与 OutlookController 默认一致）
DEFAULT_BROWSER_PROFILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'browser_profiles')
# 供 atexit / signal 在任意退出路径清空 profiles
# interrupt_requested: Ctrl+C 协作停止；summary 必须先于清理写出
_RUNTIME = {
    'controller': None,
    'profiles_root': DEFAULT_BROWSER_PROFILES,
    'cleaned': False,
    'interrupt_requested': False,
    # Web RegisterService 注入的 JobControl；CLI 为 None
    'job_control': None,
    # 主 Register 注入后，运行期结果直接写入 Mongo，不再写 Results 文件。
    'result_sink': None,
}


def _cleanup_browser_profiles_on_exit(force_dir_wipe=True):
    """进程退出时关闭浏览器并清空 browser_profiles（正常 / Ctrl+C / 多数异常退出）。

    注意：不要在 signal handler 里直接调用——应先写汇总再清理，否则会跳过 Breakdown。
    """
    ctrl = _RUNTIME.get('controller')
    if not _RUNTIME.get('cleaned'):
        _RUNTIME['cleaned'] = True
        try:
            if ctrl is not None:
                ctrl.clean_up(type='all_browser')
        except Exception:
            pass
    # 再兜底清一次目录（幂等；防止 clean_up 中途失败留下残留）
    if force_dir_wipe:
        root = _RUNTIME.get('profiles_root') or DEFAULT_BROWSER_PROFILES
        if ctrl is not None and getattr(ctrl, 'browser_user_data_root', None):
            root = ctrl.browser_user_data_root
        try:
            OutlookController.clear_browser_profiles_dir(root, log_fn=None)
        except Exception:
            pass


def _request_interrupt(signum=None, frame=None):
    """Ctrl+C / SIGTERM：只置位并唤醒主线程为 KeyboardInterrupt。

    不在此处清理浏览器 / SystemExit，否则 main 的汇总分支永远跑不到。
    汇总 + clean_up 由 main 的 except/finally 负责；atexit 兜底。
    """
    _RUNTIME['interrupt_requested'] = True
    raise KeyboardInterrupt()


def interrupt_requested():
    if bool(_RUNTIME.get('interrupt_requested')):
        return True
    control = _RUNTIME.get('job_control')
    if control is not None:
        try:
            if control.should_stop():
                return True
        except Exception:
            pass
    return False


def request_stop():
    """供 Web 服务协作停止（不抛 KeyboardInterrupt）。"""
    _RUNTIME['interrupt_requested'] = True


def clear_interrupt():
    _RUNTIME['interrupt_requested'] = False


def load_config(path='config.json'):
    """读取 config.json，支持去掉 // 行注释。"""
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read()
    lines = [line for line in raw.split('\n') if not line.strip().startswith('//')]
    return json.loads('\n'.join(lines))


class JobControl:
    """注册任务控制面：Web 服务实现 should_stop/on_log/on_stats。"""

    def should_stop(self):
        return False

    def on_log(self, line):
        pass

    def on_stats(self, runtime_stats, failure_stats, extra=None):
        pass

    def set_controller(self, controller):
        pass

    def on_batch(self, batch_index, total_succeeded, total_failed, total_submitted):
        pass


class CliControl(JobControl):
    """CLI 模式：仅依赖 _RUNTIME interrupt 与 signal。"""

    def should_stop(self):
        return bool(_RUNTIME.get('interrupt_requested'))


def append_oauth_result(
    email,
    password,
    refresh_token,
    recovery_bound=False,
    recovery_email='',
    country='',
    country_zh='',
    proxy='',
):
    # 运行时读取，避免 configure_oauth2 后仍写死导入时的 CLIENT_ID
    import controllers.oauth2 as oauth2_mod
    client_id = getattr(oauth2_mod, 'CLIENT_ID', None) or CLIENT_ID
    sink = _RUNTIME.get('result_sink')
    if sink is not None:
        sink({
            'kind': 'oauth2',
            'email': email,
            'password': password,
            'client_id': client_id,
            'refresh_token': refresh_token,
            'recovery_bound': bool(recovery_bound),
            'recovery_email': recovery_email or '',
            'country': country or '',
            'country_zh': country_zh or '',
            'proxy': proxy or '',
        })
        return
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with RESULT_WRITE_LOCK:
        with open(os.path.join(RESULTS_DIR, 'oauth2.txt'), 'a', encoding='utf-8') as f:
            f.write(f"{email}----{password}----{client_id}----{refresh_token}\n")
        try:
            from services import pool_store
            pool_store.upsert_oauth2(
                email,
                password,
                client_id,
                refresh_token,
                source='register',
                recovery_bound=bool(recovery_bound),
                recovery_email=recovery_email or '',
                country=country or '',
                country_zh=country_zh or '',
                proxy=proxy or '',
            )
        except Exception:
            pass


def append_registered_result(
    email,
    password,
    recovery_bound=False,
    recovery_email='',
    country='',
    country_zh='',
    proxy='',
):
    """注册成功进入邮箱页，但尚未拿到 OAuth2 token。"""
    sink = _RUNTIME.get('result_sink')
    if sink is not None:
        sink({
            'kind': 'registered',
            'email': email,
            'password': password,
            'recovery_bound': bool(recovery_bound),
            'recovery_email': recovery_email or '',
            'country': country or '',
            'country_zh': country_zh or '',
            'proxy': proxy or '',
        })
        return
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with RESULT_WRITE_LOCK:
        with open(os.path.join(RESULTS_DIR, 'registered.txt'), 'a', encoding='utf-8') as f:
            f.write(f"{email}----{password}\n")
        try:
            from services import pool_store
            pool_store.upsert_registered(
                email,
                password,
                source='register',
                recovery_bound=bool(recovery_bound),
                recovery_email=recovery_email or '',
                country=country or '',
                country_zh=country_zh or '',
                proxy=proxy or '',
            )
        except Exception:
            pass


def _login_and_get_token(page, email, password, prefix='', failure_hook=None, log_hook=None, current_proxy='', token_proxy_getter=None, temp_mail_cfg=None, recovery_already_bound=False, recovery_session=None):
    """
    OAuth2 Step 2: 在新浏览器(新IP)中完成完整登录 + 授权。

    冷登录常见：邮箱 →「验证电子邮件」→ 发送验证码 → #codeEntry-0..5 自动验证
    → 保持登录「否」→ consent。需 recovery_session(address+jwt) 接码。

    返回: (True, refresh_token) 或 (False, None)
    """
    from controllers.oauth2 import _handle_protect_account, _handle_proof_verify, _handle_kmsi
    # NEW 路径：若已注入注册 cookie，prefer_sso 有助于直接 consent；勿强制 sso_reload
    auth_url = build_auth_url(prefer_sso=True)
    captured_code = [None]

    def _log(stage, message, level='INFO'):
        if log_hook:
            log_hook(stage, message, level)
            return
        tag = prefix if prefix else "[OAuth2:NEW]"
        print(f"{tag}[{level}] {time.strftime('%H:%M:%S')} | {stage} | {message}")

    def on_request(request):
        code = _extract_code_from_url(request.url)
        if code:
            captured_code[0] = code

    def on_frame_navigated(frame):
        code = _extract_code_from_url(frame.url)
        if code:
            captured_code[0] = code

    page.on('request', on_request)
    page.on('framenavigated', on_frame_navigated)

    try:
        _log('start', '开始 OAuth2 (新浏览器+新IP)')
        page.goto(auth_url, timeout=AUTH_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        _settle_auth_page(page, _log, 'goto')
        _disable_auth_page_autofill(page, _log)
        _log('goto', '进入auth页面')

        state = _wait_for_auth_entry_state(page, timeout_ms=AUTH_ENTRY_TIMEOUT_MS)
        _log('entry', f'首次检测状态={state}')

        if state == 'account_type':
            state = _resolve_account_type(page, _log, captured_code=captured_code)
            _log('entry', f'帐户类型处理后状态={state}')
        if state == 'protect_account':
            state = _handle_protect_account(
                page, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                already_bound=recovery_already_bound,
            )
            _log('entry', f'保护帐户处理后状态={state}')
        if state == 'proof_verify':
            state = _handle_proof_verify(
                page, _log, temp_mail_cfg=temp_mail_cfg,
                recovery_session=recovery_session, failure_hook=failure_hook,
            )
            _log('entry', f'proof 验证后状态={state}')
        if state == 'kmsi':
            state = _handle_kmsi(page, _log)
            _log('entry', f'kmsi 处理后状态={state}')

        if state in ('login_email', 'login_password', 'account_type', 'protect_account', 'proof_verify', 'kmsi'):
            _log('entry', '新浏览器路径禁止 cookie recovery，直接进入登录流程', 'INFO')
            ok = _perform_login_after_cookie_fail(
                page,
                email,
                password,
                _log,
                failure_hook=failure_hook,
                state=state,
                captured_code=captured_code,
                temp_mail_cfg=temp_mail_cfg,
                recovery_already_bound=recovery_already_bound,
                recovery_session=recovery_session,
            )
            if not ok:
                return False, None
            if captured_code[0]:
                ok, refresh_token = _exchange_captured_code(
                    page,
                    captured_code,
                    _log,
                    failure_hook=failure_hook,
                    current_proxy=current_proxy,
                    token_proxy_getter=token_proxy_getter,
                )
                if not ok:
                    return False, None
                _log('token', 'token获取成功!', 'OK')
                return True, refresh_token
            # 登录后可能刚到 consent / 又弹出帐户类型 / 保护帐户 / proof
            state = _wait_for_auth_entry_state(page, timeout_ms=8000)
            if state == 'account_type':
                state = _resolve_account_type(page, _log, captured_code=captured_code)
            if state == 'protect_account':
                state = _handle_protect_account(
                    page, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                    already_bound=recovery_already_bound,
                )
            if state == 'proof_verify':
                state = _handle_proof_verify(
                    page, _log, temp_mail_cfg=temp_mail_cfg,
                    recovery_session=recovery_session, failure_hook=failure_hook,
                )
            if state == 'kmsi':
                state = _handle_kmsi(page, _log)
            _log('entry', f'登录后阶段={state}')

        if state == 'code' and captured_code[0]:
            ok, refresh_token = _exchange_captured_code(
                page,
                captured_code,
                _log,
                failure_hook=failure_hook,
                current_proxy=current_proxy,
                token_proxy_getter=token_proxy_getter,
            )
            if not ok:
                return False, None
            _log('token', 'token获取成功!', 'OK')
            return True, refresh_token

        if state == 'consent':
            ok, refresh_token = _click_consent_and_exchange(
                page,
                captured_code,
                _log,
                failure_hook=failure_hook,
                current_proxy=current_proxy,
                token_proxy_getter=token_proxy_getter,
            )
            if not ok:
                return False, None
            _log('token', 'token获取成功!', 'OK')
            return True, refresh_token

        if failure_hook:
            failure_hook('oauth_consent_fail')
        _dump_auth_page(page, _log)
        _log('entry', f'未进入同意或登录页面，最终状态={state}', 'FAIL')
        return False, None

    except Exception as e:
        _log('exception', f'异常: {e}', 'FAIL')
        return False, None

    finally:
        page.remove_listener('request', on_request)
        page.remove_listener('framenavigated', on_frame_navigated)


def process_single_flow(controller, task_num=0, total=0):
    
    page = None
    t_start = time.time()
    task_ok = False
    progress_noted = False

    def _note(ok):
        nonlocal progress_noted
        if progress_noted:
            return
        progress_noted = True
        try:
            controller.note_task_finished(ok, total)
        except Exception:
            pass

    try:
        controller.set_task_prefix(task_num, total)
        page = controller.get_thread_page()
        if not page:
            controller.log_event('REGISTER', 'FAIL', 'bootstrap', "浏览器页面创建失败，跳过当前任务")
            _note(False)
            return False
        current_proxy = getattr(controller.thread_local, '_proxy', '')
        email = random_email()
        password = generate_strong_password()
        controller.log_event('REGISTER', 'INFO', 'account', f"Generate {email}{controller.email_suffix}")

        # 注册微软邮箱: True / False / 'handed_off'(策略2 仅到验证码后交由人工)
        result = controller.outlook_register(page, email, password)

        # 策略 2：只自动到验证码界面，过码 + OAuth 全由人工，程序不再跑 OAuth
        if result == 'handed_off':
            controller.log_event(
                'REGISTER', 'OK', 'finish',
                f"策略2交接完成(验证码起由人工) ({time.time()-t_start:.0f}s)",
            )
            task_ok = True
            _note(True)
            return True
        # 如果注册失败，直接返回false
        if not result:
            controller.log_event('REGISTER', 'FAIL', 'finish', f"注册失败 ({time.time()-t_start:.0f}s)")
            _note(False)
            return False

        # 拼接完整的outlook或者hotmail邮箱
        full_email = f"{email}{controller.email_suffix}"
        rec_status = controller.recovery_bind_status()
        recovery_session = rec_status.get('session')
        recovery_email_addr = (recovery_session or {}).get('address') or ''

        # 注册成功进入邮箱页：先入「注册成功」池（OAuth 成功后再升格）
        ip_info = getattr(controller.thread_local, '_ip_info', None) or {}
        reg_country = str(ip_info.get('country') or '').strip().upper()
        if reg_country in ('??', 'XX', 'ZZ'):
            reg_country = ''
        reg_country_zh = str(ip_info.get('country_zh') or '').strip()
        reg_proxy = current_proxy or getattr(controller.thread_local, '_proxy', '') or ''
        try:
            append_registered_result(
                full_email,
                password,
                recovery_bound=bool(rec_status.get('bound')),
                recovery_email=recovery_email_addr,
                country=reg_country,
                country_zh=reg_country_zh,
                proxy=reg_proxy,
            )
        except Exception:
            pass

        # 如果注册成功且不需要Oauth2，则直接返回true
        if not controller.enable_oauth2:
            controller.log_event('REGISTER', 'OK', 'finish', f"成功 ({time.time()-t_start:.0f}s)")
            task_ok = True
            _note(True)
            return True

        controller.log_event('OAUTH_COOKIE', 'INFO', 'start', f"注册完成，开始OAuth2: {full_email}", attempt=1)
        controller.log_event(
            'OAUTH_COOKIE', 'INFO', 'recovery_status',
            f"注册阶段 recovery_bound={rec_status['bound']} skipped={rec_status['skipped']} "
            f"session_addr={recovery_email_addr}",
            attempt=1,
        )
        oauth_ok, token = get_oauth2_token(
            page,
            full_email,
            password,
            RESULTS_DIR,
            failure_hook=controller.bump_failure,
            log_hook=controller.make_logger('OAUTH_COOKIE', attempt=1),
            current_proxy=current_proxy,
            token_proxy_getter=lambda exclude='': controller.fresh_proxy_url(exclude=exclude),
            temp_mail_cfg=getattr(controller, 'temp_mail_cfg', None),
            recovery_already_bound=rec_status['bound'],
            recovery_session=recovery_session,
        )

        # 拿到 token 后立刻记进度（在 clean_up 之前）
        if oauth_ok:
            append_oauth_result(
                full_email, password, token,
                recovery_bound=bool(rec_status.get('bound')),
                recovery_email=recovery_email_addr,
                country=reg_country,
                country_zh=reg_country_zh,
                proxy=reg_proxy,
            )
            controller.log_event('OAUTH_COOKIE', 'OK', 'finish', f"OAuth2 token获取成功 ({time.time()-t_start:.0f}s)", attempt=1)
            task_ok = True
            _note(True)
            return True

        # COOKIE 路径失败：优先再同浏览器重试 1 次（多等 cookie），避免立刻丢掉 SSO
        controller.log_event('OAUTH_COOKIE', 'WARN', 'retry_same', '同浏览器再试 OAuth 一次（沉淀 cookie 后）', attempt=1)
        try:
            page.wait_for_timeout(7000)
        except Exception:
            pass
        oauth_ok, token = get_oauth2_token(
            page,
            full_email,
            password,
            RESULTS_DIR,
            failure_hook=controller.bump_failure,
            log_hook=controller.make_logger('OAUTH_COOKIE', attempt=2),
            current_proxy=current_proxy,
            token_proxy_getter=lambda exclude='': controller.fresh_proxy_url(exclude=exclude),
            temp_mail_cfg=getattr(controller, 'temp_mail_cfg', None),
            recovery_already_bound=controller.recovery_bind_status()['bound'],
            recovery_session=controller.recovery_bind_status().get('session'),
        )
        if oauth_ok:
            append_oauth_result(
                full_email, password, token,
                recovery_bound=bool(controller.recovery_bind_status().get('bound')),
                recovery_email=((controller.recovery_bind_status().get('session') or {}).get('address') or recovery_email_addr),
                country=reg_country,
                country_zh=reg_country_zh,
                proxy=reg_proxy,
            )
            controller.log_event('OAUTH_COOKIE', 'OK', 'finish', f"OAuth2 token获取成功(同浏览器重试) ({time.time()-t_start:.0f}s)", attempt=2)
            task_ok = True
            _note(True)
            return True

        # 仍失败：导出 storage_state 再开新浏览器注入 cookie（比纯空浏览器好）
        # 注意：纯 NEW 无 cookie 时常见「找不到帐户 / 密码登录不可用 / 密钥页」
        storage_state = None
        try:
            storage_state = page.context.storage_state()
            controller.log_event('OAUTH_NEW', 'INFO', 'cookie_export', f"已导出 storage_state cookies={len(storage_state.get('cookies') or [])}")
        except Exception as exc:
            controller.log_event('OAUTH_NEW', 'WARN', 'cookie_export', f"导出 cookie 失败: {exc}")

        controller.clean_up(page, "done_browser")
        page = None

        # 最多 2 次 NEW（比原先 3 次更克制）；优先带 cookie 启动
        for attempt in range(1, 3):
            if time.time() - t_start > 600:
                controller.log_event('OAUTH_NEW', 'FAIL', 'timeout', f"任务超10分钟，放弃 ({time.time()-t_start:.0f}s)", attempt=attempt)
                controller.penalize_ip(penalty=2)
                _note(False)
                return False
            controller.set_task_prefix(task_num, total)
            controller.log_event(
                'OAUTH_NEW', 'WARN', 'retry',
                f"OAuth2 新环境重试 {attempt}/2（注入注册 cookie={bool(storage_state)}）...",
                attempt=attempt,
            )
            try:
                page = controller.get_thread_page()
                if not page:
                    controller.log_event('OAUTH_NEW', 'WARN', 'page', f"重试 {attempt} 获取页面失败，等待后重试...", attempt=attempt)
                    time.sleep(5)
                    continue
                if storage_state:
                    try:
                        # 注入注册会话 cookie，避免「找不到该用户名」的冷启动
                        page.context.add_cookies(storage_state.get('cookies') or [])
                        controller.log_event('OAUTH_NEW', 'INFO', 'cookie_import', f"已注入 cookies={len(storage_state.get('cookies') or [])}", attempt=attempt)
                    except Exception as exc:
                        controller.log_event('OAUTH_NEW', 'WARN', 'cookie_import', f"注入 cookie 失败: {exc}", attempt=attempt)
                ok, token = _login_and_get_token(
                    page,
                    full_email,
                    password,
                    prefix=controller._log_prefix_str(),
                    failure_hook=controller.bump_failure,
                    log_hook=controller.make_logger('OAUTH_NEW', attempt=attempt),
                    current_proxy=getattr(controller.thread_local, '_proxy', ''),
                    token_proxy_getter=lambda exclude='': controller.fresh_proxy_url(exclude=exclude),
                    temp_mail_cfg=getattr(controller, 'temp_mail_cfg', None),
                    recovery_already_bound=controller.recovery_bind_status()['bound'],
                    recovery_session=controller.recovery_bind_status().get('session'),
                )
                if ok:
                    append_oauth_result(
                        full_email, password, token,
                        recovery_bound=bool(controller.recovery_bind_status().get('bound')),
                        recovery_email=((controller.recovery_bind_status().get('session') or {}).get('address') or recovery_email_addr),
                        country=reg_country,
                        country_zh=reg_country_zh,
                        proxy=reg_proxy,
                    )
                    controller.log_event('OAUTH_NEW', 'OK', 'finish', f"OAuth2 token获取成功 ({time.time()-t_start:.0f}s)", attempt=attempt)
                    task_ok = True
                    _note(True)
                    return True
            except Exception as e:
                from controllers.oauth2 import _compact_exc
                controller.log_event('OAUTH_NEW', 'FAIL', 'exception', f"重试异常: {_compact_exc(e)}", attempt=attempt)
            finally:
                if page:
                    try:
                        controller.clean_up(page, "done_browser")
                    except Exception:
                        pass
                    page = None
                time.sleep(3)

        controller.bump_failure('oauth_retry_exhausted')
        controller.log_event('OAUTH_NEW', 'FAIL', 'finish', f"OAuth2 同浏览器+新环境均失败 ({time.time()-t_start:.0f}s)", attempt=2)
        controller.penalize_ip(penalty=2)
        _note(False)
        return False

    except Exception as e:
        from controllers.oauth2 import _compact_exc
        controller.log_event('REGISTER', 'FAIL', 'exception', f"异常 ({time.time()-t_start:.0f}s): {_compact_exc(e)}")
        _note(False)
        return False
    finally:
        if not progress_noted:
            _note(task_ok)
        if page:
            controller.clean_up(page, "done_browser")


def build_summary_lines(controller, tasks, succeeded_tasks, failed_tasks, elapsed_total, interrupted=False):
    fs = controller.failure_stats
    total_done = succeeded_tasks + failed_tasks
    ip_fail = fs['ip_cant_open'] + fs['ip_blocked']
    captcha_reached = total_done - ip_fail
    oauth_success = succeeded_tasks
    lines = [
        "",
        "=" * 50,
        f"[Result] 状态: {'INTERRUPTED' if interrupted else 'DONE'}",
        f"[Result] 总任务目标: {tasks}",
        f"[Result] 已完成: {total_done}",
        f"[Result] 成功: {succeeded_tasks}/{max(total_done, 1)} ({succeeded_tasks / max(total_done, 1) * 100:.0f}%)",
        f"[Result] 耗时: {elapsed_total / 60:.1f}min",
        "─" * 50,
        f"[Breakdown] IP打不开页面:   {fs['ip_cant_open']:>3}个 ({fs['ip_cant_open']/max(total_done, 1)*100:5.1f}%)",
        f"[Breakdown] IP被风控拦截:   {fs['ip_blocked']:>3}个 ({fs['ip_blocked']/max(total_done, 1)*100:5.1f}%)",
        f"[Breakdown] IP失败合计:     {ip_fail:>3}个 ({ip_fail/max(total_done, 1)*100:5.1f}%)",
        f"[Breakdown] 验证码未通过:    {fs['captcha_fail']:>3}个",
        f"[Breakdown] btn2从未出现:    {fs['captcha_btn2_never_appeared']:>3}个",
        f"[Breakdown] btn2出现后失败:  {fs['captcha_btn2_appeared_but_failed']:>3}个",
        f"[Breakdown] FunCaptcha:      {fs['funcaptcha']:>3}个",
        f"[Breakdown] 注册页打开失败:  {fs['register_page_open_fail']:>3}个",
        f"[Breakdown] 注册流程失败:    {fs['register_form_fail']:>3}个",
        f"[Breakdown] 浏览器启动失败:  {fs['browser_launch_fail']:>3}个",
        f"[Breakdown] Context创建失败: {fs['browser_context_fail']:>3}个",
        f"[Breakdown] Page创建失败:    {fs['browser_page_fail']:>3}个",
        f"[Breakdown] Playwright异常:  {fs['playwright_runtime_fail']:>3}个",
        f"[Breakdown] 邮箱未初始化:    {fs['mail_init_fail']:>3}个",
        f"[Breakdown] OAuth登录超时:   {fs['oauth_login_timeout']:>3}个",
        f"[Breakdown] OAuth同意失败:   {fs['oauth_consent_fail']:>3}个",
        f"[Breakdown] OAuth密码错误:   {fs.get('oauth_password_wrong', 0):>3}个",
        f"[Breakdown] OAuth密码不可用: {fs.get('oauth_password_blocked', 0):>3}个",
        f"[Breakdown] 备用邮箱绑定失败: {fs.get('recovery_bind_fail', 0):>3}个",
        f"[Breakdown] OAuth抓码失败:   {fs['oauth_code_fail']:>3}个",
        f"[Breakdown] OAuth网络失败:   {fs['oauth_token_network_fail']:>3}个",
        f"[Breakdown] OAuth换token失败:{fs['oauth_token_fail']:>3}个",
        f"[Breakdown] OAuth重试耗尽:   {fs['oauth_retry_exhausted']:>3}个",
        "─" * 50,
    ]
    if captcha_reached > 0:
        lines.append(f"[Breakdown] 到达验证码:      {captcha_reached:>3}个")
        lines.append(f"[Breakdown] 剔除IP后成功率:   {oauth_success}/{captcha_reached} ({oauth_success/captcha_reached*100:.0f}%)")
    lines.append("=" * 50)
    return lines


def build_cumulative_lines(total_succeeded, total_failed, total_elapsed, batch_index):
    total_done = total_succeeded + total_failed
    return [
        "─" * 50,
        f"[Cumulative] 批次: {batch_index}",
        f"[Cumulative] 已完成: {total_done}",
        f"[Cumulative] 成功: {total_succeeded}/{max(total_done, 1)} ({total_succeeded / max(total_done, 1) * 100:.0f}%)",
        f"[Cumulative] 耗时: {total_elapsed / 60:.1f}min",
        "─" * 50,
    ]


def _collect_done_futures(running_futures, succeeded_tasks, failed_tasks):
    """收集已完成 future，返回 (running, succeeded, failed, got_any)。"""
    done_futures = {f for f in running_futures if f.done()}
    if not done_futures:
        return running_futures, succeeded_tasks, failed_tasks, False
    for future in done_futures:
        try:
            if future.result():
                succeeded_tasks += 1
            else:
                failed_tasks += 1
        except Exception:
            failed_tasks += 1
        running_futures.discard(future)
    return running_futures, succeeded_tasks, failed_tasks, True


def run_concurrent_flows(
    controller,
    concurrent_flows=10,
    tasks=100,
    success_tasks=None,
    task_offset=0,
    progress_total=None,
    progress_base_succeeded=0,
    progress_base_failed=0,
    run_started_at=None,
    drain_timeout_sec=180,
    stall_timeout_sec=600,
):
    """
    并发任务调度器。

    停止条件（停投递；在途任务限时收尾）：
    - tasks: 本批最多提交数
    - success_tasks: 本批成功数目标（None=不按成功数截断）

    注意：成功目标只看「已完成成功数」，不用「成功+在途」预占名额
    （预占会在成功 299、剩余 1 个在途卡住时永久停住）。
    在途收尾有超时；超时后取消 future 并强制关浏览器，避免卡死整批。

    progress_base_* / run_started_at：跨批次累计展示（成功/失败/耗时连续）。
    """
    task_counter = 0
    succeeded_tasks = 0  # 本批成功（用于 batch_success_limit）
    failed_tasks = 0
    t_batch_start = time.time()
    run_started = run_started_at if run_started_at is not None else t_batch_start
    base_ok = int(progress_base_succeeded or 0)
    base_fail = int(progress_base_failed or 0)
    display_total = progress_total if progress_total is not None else tasks
    last_progress_at = t_batch_start
    # 展示用累计；本批判定仍用 succeeded_tasks / failed_tasks
    if hasattr(controller, 'set_progress_base'):
        controller.set_progress_base(base_ok, base_fail, run_started)
    else:
        controller.update_runtime_stats(
            started_at=run_started,
            submitted=0,
            running=0,
            succeeded=base_ok,
            failed=base_fail,
        )

    def _sync_stats(running_n):
        # runtime_stats 写累计值，保证 [进度] 与中断快照跨批连续
        controller.update_runtime_stats(
            submitted=task_counter,
            running=running_n,
            succeeded=base_ok + succeeded_tasks,
            failed=base_fail + failed_tasks,
            started_at=run_started,
        )

    def _force_abandon(running_futures, reason, timeout_sec):
        nonlocal succeeded_tasks, failed_tasks
        abandoned = len(running_futures)
        controller.log_plain(
            f"[Batch][WARN] {reason}（{timeout_sec}s），放弃 {abandoned} 个未完成 "
            f"(batch_success={succeeded_tasks}"
            f"{'/' + str(success_tasks) if success_tasks is not None else ''})"
        )
        for fut in list(running_futures):
            fut.cancel()
        try:
            # 强制关浏览器，让卡在 Playwright 的线程尽快抛错退出
            controller.clean_up(type="all_browser")
        except Exception:
            pass
        t_end = time.time() + 15
        while running_futures and time.time() < t_end:
            running_futures, succeeded_tasks, failed_tasks, got = _collect_done_futures(
                running_futures, succeeded_tasks, failed_tasks
            )
            if got:
                _sync_stats(len(running_futures))
            else:
                time.sleep(0.2)
        leftover = len(running_futures)
        if leftover:
            failed_tasks += leftover
            running_futures.clear()
            controller.log_plain(
                f"[Batch][WARN] 仍有 {leftover} 个任务未返回，记为失败并继续下一批"
            )
        _sync_stats(0)
        return running_futures

    # 不用 with：默认 shutdown(wait=True) 会在卡死线程上永久阻塞
    executor = ThreadPoolExecutor(max_workers=concurrent_flows)
    running_futures = set()
    stop_submit = False
    try:
        while True:
            running_futures, succeeded_tasks, failed_tasks, got = _collect_done_futures(
                running_futures, succeeded_tasks, failed_tasks
            )
            if got:
                last_progress_at = time.time()
                _sync_stats(len(running_futures))

            if success_tasks is not None and succeeded_tasks >= success_tasks:
                stop_submit = True
            if task_counter >= tasks:
                stop_submit = True

            if not stop_submit:
                # 只按已完成成功数判断，允许并发略超 batch 目标，避免「成功+在途」预占卡死
                while (
                    len(running_futures) < concurrent_flows
                    and task_counter < tasks
                    and (success_tasks is None or succeeded_tasks < success_tasks)
                ):
                    task_counter += 1
                    global_num = task_offset + task_counter
                    new_future = executor.submit(
                        process_single_flow, controller, global_num, display_total
                    )
                    running_futures.add(new_future)
                    _sync_stats(len(running_futures))
                    if display_total > 1 and global_num % max(display_total // 2, 1) == 0:
                        controller.log_plain(f"已提交 {global_num}/{display_total} 任务.")
                    last_progress_at = time.time()

            if stop_submit:
                if not running_futures:
                    break
                idle = time.time() - last_progress_at
                if idle >= drain_timeout_sec:
                    running_futures = _force_abandon(
                        running_futures, "在途收尾超时", drain_timeout_sec
                    )
                    break
            else:
                if not running_futures and task_counter >= tasks:
                    break
                # 未达成功目标但在途全卡住：限时放弃，避免永远停在 299
                if running_futures and (time.time() - last_progress_at) >= stall_timeout_sec:
                    running_futures = _force_abandon(
                        running_futures, "在途任务停滞", stall_timeout_sec
                    )
                    break

            # 协作式中断：信号只置位 + KeyboardInterrupt；此处尽快停投递并退出循环
            if interrupt_requested():
                stop_submit = True
                controller.log_plain(
                    "[Signal][WARN] run_concurrent_flows 检测到中断请求，停止提交并收尾在途计数"
                )
                # 尽量收集已完成的，避免进度少算；未完成的不无限等
                running_futures, succeeded_tasks, failed_tasks, got = _collect_done_futures(
                    running_futures, succeeded_tasks, failed_tasks
                )
                if got:
                    _sync_stats(len(running_futures))
                leftover = len(running_futures)
                if leftover:
                    # 在途任务稍后会被 clean_up 掐断；计数上记失败，避免汇总空白
                    failed_tasks += leftover
                    running_futures.clear()
                    controller.log_plain(
                        f"[Signal][WARN] 中断时放弃 {leftover} 个在途任务（记为失败）"
                    )
                _sync_stats(0)
                break

            time.sleep(0.2)
    except KeyboardInterrupt:
        _sync_stats(len(running_futures))
        controller.log_plain("[Signal][WARN] run_concurrent_flows 收到 Ctrl+C，停止继续提交任务")
        # 尽量同步已完成结果；在途记失败
        try:
            running_futures, succeeded_tasks, failed_tasks, _ = _collect_done_futures(
                running_futures, succeeded_tasks, failed_tasks
            )
            leftover = len(running_futures)
            if leftover:
                failed_tasks += leftover
                running_futures.clear()
        except Exception:
            pass
        _sync_stats(0)
        # 不 re-raise：把本批已有计数带回 main 写汇总，再由 main 清理
        _RUNTIME['interrupt_requested'] = True
    finally:
        try:
            executor.shutdown(wait=_RUNTIME.get('result_sink') is not None, cancel_futures=True)
        except TypeError:
            # Python < 3.9 无 cancel_futures
            executor.shutdown(wait=_RUNTIME.get('result_sink') is not None)
        except Exception:
            pass

    elapsed_batch = time.time() - t_batch_start
    controller.update_runtime_stats(
        submitted=task_counter,
        running=0,
        succeeded=base_ok + succeeded_tasks,
        failed=base_fail + failed_tasks,
        started_at=run_started,
    )
    # 中断时本批 Breakdown 由 main 用 interrupted=True 再打一次，避免重复
    if not interrupt_requested():
        for line in build_summary_lines(controller, display_total, succeeded_tasks, failed_tasks, elapsed_batch):
            controller.log_plain(line)
    return succeeded_tasks, failed_tasks


def run_registration_job(data, control=None, clear_profiles=True, install_signals=False, result_sink=None):
    """可调用的注册批循环（CLI 与 Web 共用）。

    Args:
        data: 已解析的 config dict
        control: JobControl 实例；Web 服务注入以支持 stop/stats 回调
        result_sink: 主服务结果回调；设置后不写入 legacy Results 文件。
        clear_profiles: 启动前是否清空 browser_profiles
        install_signals: 是否注册 SIGINT/SIGTERM（仅 CLI 为 True；Web 勿装）

    Returns:
        dict: succeeded / failed / submitted / batches / elapsed / interrupted / log_path
    """
    control = control or CliControl()
    _RUNTIME['job_control'] = control
    _RUNTIME['result_sink'] = result_sink
    clear_interrupt()
    _RUNTIME['cleaned'] = False

    # 应用 oauth2 配置（覆盖模块级常量）
    try:
        configure_oauth2(data.get('oauth2') or {})
    except Exception:
        pass

    # headless 始终以 config / Web UI 为准，不再被 FORCE_HEADLESS 强制覆盖。
    # FORCE_HEADLESS 仅作提示：Docker 无桌面时建议 config headless=true。
    force_raw = str(os.environ.get('FORCE_HEADLESS', '')).strip().lower()
    force_hint = force_raw in ('1', 'true', 'yes', 'on')
    headless = bool(data.get('headless'))
    if force_hint and not headless:
        print(
            f"[WARN] FORCE_HEADLESS={force_raw or '1'} 但 config headless=false；"
            f"已按配置有头运行（不再强制无头）。无桌面环境请自行打开 headless。"
        )
    else:
        print(f"[OK] headless={headless}（FORCE_HEADLESS={force_raw or '0'}，仅提示不强制）")

    if install_signals:
        for _sig in (
            getattr(signal, 'SIGINT', None),
            getattr(signal, 'SIGTERM', None),
            getattr(signal, 'SIGBREAK', None),
        ):
            if _sig is None:
                continue
            try:
                signal.signal(_sig, _request_interrupt)
            except Exception:
                pass

    browser_cfg = data.get('browser') or {}
    profiles_root = (browser_cfg.get('user_data_root') or '').strip() or DEFAULT_BROWSER_PROFILES
    _RUNTIME['profiles_root'] = profiles_root
    if clear_profiles:
        OutlookController.clear_browser_profiles_dir(profiles_root, log_fn=None)

    # 类级代理缓存必须按本次 config 重建，否则 Web 改代理不生效
    OutlookController.reset_shared_state()
    OutlookController._proxy_config = None

    tasks = max(1, int(data["tasks"]))
    raw_success = data.get("success_tasks")
    success_tasks = None if raw_success is None else max(1, int(raw_success))
    concurrent_flows = max(1, int(data["concurrent_flows"]))
    batch_success_limit = max(1, int(data.get("batch_success_limit", 300)))

    total_succeeded = 0
    total_failed = 0
    total_submitted = 0
    total_elapsed = 0.0
    batch_index = 0
    interrupted = False
    selected_controller = None
    run_started_at = time.time()
    log_path = None

    def _emit_stats(controller, extra=None):
        if controller is None:
            return
        try:
            runtime = controller.get_runtime_stats()
            failures = (
                controller.get_failure_stats()
                if hasattr(controller, 'get_failure_stats')
                else dict(getattr(controller, 'failure_stats', {}) or {})
            )
            payload = {
                'batch_index': batch_index,
                'total_succeeded': total_succeeded,
                'total_failed': total_failed,
                'total_submitted': total_submitted,
                'tasks': tasks,
                'success_tasks': success_tasks,
                'concurrent_flows': concurrent_flows,
                'log_path': getattr(controller, 'log_path', None),
            }
            if extra:
                payload.update(extra)
            control.on_stats(runtime, failures, payload)
        except Exception:
            pass

    try:
        while True:
            if interrupt_requested():
                interrupted = True
                break

            remaining_tasks = tasks - total_submitted
            if remaining_tasks <= 0:
                break
            if success_tasks is not None and total_succeeded >= success_tasks:
                break

            remaining_success = None if success_tasks is None else (success_tasks - total_succeeded)
            if remaining_success is not None and remaining_success <= 0:
                break

            if remaining_success is None:
                batch_target = batch_success_limit
            else:
                batch_target = min(batch_success_limit, remaining_success)

            batch_index += 1
            selected_controller = OutlookController(data)
            _RUNTIME['controller'] = selected_controller
            _RUNTIME['profiles_root'] = selected_controller.browser_user_data_root
            _RUNTIME['cleaned'] = False
            log_path = selected_controller.log_path
            try:
                control.set_controller(selected_controller)
            except Exception:
                pass
            selected_controller.set_progress_base(total_succeeded, total_failed, run_started_at)
            _emit_stats(selected_controller, {'status': 'running'})

            if batch_index == 1:
                selected_controller.log_plain(f"[Log] 本次日志文件: {selected_controller.log_path}")
                selected_controller.log_plain(
                    "[Batch] 代理为固定出口 IP；批次重启仅刷新程序内权重/统计，不会更换外部出口 IP"
                )
                selected_controller.log_plain(
                    f"[Batch] 全局结束条件: tasks={tasks} 或 success_tasks="
                    f"{success_tasks if success_tasks is not None else 'null(不限)'} 任一达标即停; "
                    f"batch_success_limit={batch_success_limit}"
                )
                selected_controller.log_plain(
                    "[Batch] 满 batch_success_limit 后清 IP/代理权重并开下一批；"
                    "累计成功/失败/耗时/[进度] 连续累加，不因换批归零"
                )
            rem_s = remaining_success if remaining_success is not None else "unlimited"
            selected_controller.log_plain(
                f"[Batch] start index={batch_index} batch_success_target={batch_target} "
                f"remaining_tasks={remaining_tasks} remaining_success={rem_s} "
                f"cumulative_success={total_succeeded} cumulative_submitted={total_submitted}"
            )

            batch_succeeded = 0
            batch_failed = 0
            batch_interrupted = False
            try:
                batch_succeeded, batch_failed = run_concurrent_flows(
                    selected_controller,
                    concurrent_flows,
                    tasks=remaining_tasks,
                    success_tasks=batch_target,
                    task_offset=total_submitted,
                    progress_total=tasks,
                    progress_base_succeeded=total_succeeded,
                    progress_base_failed=total_failed,
                    run_started_at=run_started_at,
                )
                if interrupt_requested():
                    batch_interrupted = True
                    interrupted = True
            except KeyboardInterrupt:
                interrupted = True
                batch_interrupted = True
                _RUNTIME['interrupt_requested'] = True
                selected_controller.log_plain(
                    "[Signal][WARN] 检测到 Ctrl+C，准备写入中断汇总并清理资源"
                )
                if batch_succeeded == 0 and batch_failed == 0:
                    snapshot = selected_controller.get_runtime_stats()
                    cum_ok = snapshot.get('succeeded', total_succeeded)
                    cum_fail = snapshot.get('failed', total_failed)
                    batch_succeeded = max(0, int(cum_ok) - total_succeeded)
                    batch_failed = max(0, int(cum_fail) - total_failed)

            batch_elapsed = time.time() - run_started_at - total_elapsed
            if batch_elapsed < 0:
                batch_elapsed = 0.0

            if batch_interrupted:
                selected_controller.log_plain(
                    "[Signal][WARN] 中断汇总：以下为当前批次/累计统计（在途任务可能未完全计入成功）"
                )
                for line in build_summary_lines(
                    selected_controller, tasks, batch_succeeded, batch_failed, batch_elapsed, interrupted=True
                ):
                    selected_controller.log_plain(line)

            total_succeeded += batch_succeeded
            total_failed += batch_failed
            batch_done = batch_succeeded + batch_failed
            total_submitted += batch_done
            total_elapsed += batch_elapsed
            for line in build_cumulative_lines(total_succeeded, total_failed, total_elapsed, batch_index):
                selected_controller.log_plain(line)

            try:
                control.on_batch(batch_index, total_succeeded, total_failed, total_submitted)
            except Exception:
                pass
            _emit_stats(selected_controller)

            if interrupted:
                selected_controller.log_plain(
                    f"[Batch] 中断退出 total_success={total_succeeded} total_failed={total_failed} "
                    f"total_submitted={total_submitted} batches={batch_index} "
                    f"elapsed={total_elapsed / 60:.1f}min"
                )
                selected_controller.log_plain(f"[Log] 已写入: {selected_controller.log_path}")
                selected_controller.log_plain("[Signal][WARN] 开始清理浏览器与 browser_profiles…")
                try:
                    selected_controller.clean_up(type="all_browser")
                except Exception:
                    pass
                _RUNTIME['cleaned'] = True
                break

            try:
                selected_controller.clean_up(type="all_browser")
            except Exception:
                pass
            _RUNTIME['cleaned'] = True

            if success_tasks is not None and total_succeeded >= success_tasks:
                selected_controller.log_plain(
                    f"[Batch] stop: success_tasks 达标 ({total_succeeded}/{success_tasks})"
                )
                break
            if total_submitted >= tasks:
                selected_controller.log_plain(
                    f"[Batch] stop: tasks 达标 ({total_submitted}/{tasks})"
                )
                break
            if batch_done <= 0:
                selected_controller.log_plain("[Batch] stop: 本批无完成任务，避免空转")
                break

            OutlookController.reset_shared_state()
            selected_controller.log_plain(
                f"[Batch] reset index={batch_index} "
                f"cumulative_success={total_succeeded} cumulative_failed={total_failed} "
                f"cumulative_submitted={total_submitted}"
            )

        if selected_controller and not interrupted:
            selected_controller.log_plain(
                f"[Batch] 结束 total_success={total_succeeded} total_failed={total_failed} "
                f"total_submitted={total_submitted} batches={batch_index} "
                f"elapsed={total_elapsed / 60:.1f}min"
            )
            selected_controller.log_plain(f"[Log] 已写入: {selected_controller.log_path}")
    except KeyboardInterrupt:
        interrupted = True
        _RUNTIME['interrupt_requested'] = True
        ctrl = _RUNTIME.get('controller') or selected_controller
        if ctrl is not None:
            try:
                ctrl.log_plain("[Signal][WARN] 主循环外收到 Ctrl+C，写入可用汇总后退出")
                snap = ctrl.get_runtime_stats()
                cum_ok = int(snap.get('succeeded', total_succeeded) or total_succeeded)
                cum_fail = int(snap.get('failed', total_failed) or total_failed)
                if cum_ok >= total_succeeded:
                    show_ok, show_fail = cum_ok, cum_fail
                else:
                    show_ok, show_fail = total_succeeded, total_failed
                show_done = show_ok + show_fail
                elapsed = time.time() - run_started_at
                for line in build_cumulative_lines(show_ok, show_fail, elapsed, max(batch_index, 1)):
                    ctrl.log_plain(line)
                ctrl.log_plain(
                    f"[Batch] 中断退出 total_success={show_ok} total_failed={show_fail} "
                    f"total_submitted={show_done} batches={batch_index} "
                    f"elapsed={elapsed / 60:.1f}min"
                )
                if getattr(ctrl, 'log_path', None):
                    ctrl.log_plain(f"[Log] 已写入: {ctrl.log_path}")
                total_succeeded, total_failed = show_ok, show_fail
                total_submitted = show_done
                total_elapsed = elapsed
                log_path = getattr(ctrl, 'log_path', log_path)
            except Exception:
                pass
    finally:
        _emit_stats(
            _RUNTIME.get('controller') or selected_controller,
            {
                'status': 'interrupted' if interrupted else 'done',
                'finished': True,
            },
        )
        try:
            _cleanup_browser_profiles_on_exit()
        except Exception:
            pass
        _RUNTIME['job_control'] = None
        _RUNTIME['result_sink'] = None
        try:
            control.set_controller(None)
        except Exception:
            pass

    return {
        'succeeded': total_succeeded,
        'failed': total_failed,
        'submitted': total_submitted,
        'batches': batch_index,
        'elapsed': total_elapsed if total_elapsed > 0 else (time.time() - run_started_at),
        'interrupted': interrupted,
        'log_path': log_path,
    }


if __name__ == "__main__":
    atexit.register(_cleanup_browser_profiles_on_exit)
    data = load_config('config.json')
    run_registration_job(data, control=CliControl(), clear_profiles=True, install_signals=True)
