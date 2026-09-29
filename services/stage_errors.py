"""Small public error vocabulary; private worker messages never reach chats."""
MESSAGES={
    'stage_timeout':'该计算阶段超过单独时限；已完成分块保留。',
    'unit_timeout':'systemd 明确报告该阶段触及服务硬时限；已完成分块保留。',
    'unit_oom':'systemd 明确报告该阶段因内存不足被终止；不是 QQ 发送失败。',
    'unit_signal':'该阶段被信号终止；没有证据将其归因于内存不足。',
    'unit_exit':'该阶段服务非零退出；具体诊断保留在私有日志。',
    'stage_exit':'该阶段子进程非零退出；具体诊断保留在私有日志。',
    'stage_terminated':'该阶段收到终止请求；已完成分块保留。',
    'inference_busy':'共享推理锁被占用，本阶段没有开始计算。',
    'unsealed_output':'发现未完成输出，已保留；需要明确的恢复决定，不能覆盖重跑。',
    'checkpoint_error':'阶段文件或收据校验失败，拒绝覆盖或复用。',
    'executor_error':'阶段执行器发生异常；具体诊断保留在私有日志。',
    'unit_result_missing':'阶段已退出，但缺少可验证的阶段结果；不推断为内存或发送失败。',
    'unit_result_invalid':'阶段结果格式或身份无效，不能据此标记成功。',
    'stage_interrupted':'阶段已停止但没有完整输出凭据；已完成分块保留。',
}
WORKER_CODES={'stage_timeout','stage_exit','stage_terminated','inference_busy',
              'unsealed_output','checkpoint_error','executor_error'}
INTERRUPTED_CODES={'stage_timeout','unit_timeout','unit_signal','stage_terminated',
                   'inference_busy','unsealed_output','unit_result_missing','stage_interrupted'}


def failure_document(code,stage):
    if code not in MESSAGES:
        code='executor_error'
    return {'code':code,'stage':stage,'message':MESSAGES[code]}


def classify_failure(*,stage,unit,recipe,witness,status):
    """Use systemd's diagnosis first, then an identity-bound worker status.

    Neither a signal nor a missing result is evidence of OOM. A status from
    another unit/recipe (including a prior retry) must never supply the cause.
    """
    service=witness.get('service',{}) if isinstance(witness,dict) else {}
    result=service.get('Result') if isinstance(service,dict) else None
    systemd_codes={'oom-kill':'unit_oom','timeout':'unit_timeout',
                   'signal':'unit_signal','core-dump':'unit_signal'}
    if result in systemd_codes:
        return failure_document(systemd_codes[result],stage)
    if (isinstance(status,dict) and status.get('stage')==stage and status.get('unit')==unit
            and status.get('recipe')==recipe and status.get('state')=='failed'
            and status.get('code') in WORKER_CODES):
        return failure_document(status['code'],stage)
    if result=='exit-code':
        return failure_document('unit_exit',stage)
    return failure_document('unit_result_missing' if status is None else 'unit_result_invalid',stage)
