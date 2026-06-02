"""Scheduler registration for Key Portal background work."""


def configure_scheduler(scheduler, config, jobs):
    scheduler.add_job(
        jobs["expiry_check"],
        "interval",
        minutes=config.KEY_CHECK_INTERVAL_MINUTES,
        id="expiry_check",
    )

    scheduler.add_job(
        jobs["usage_broadcast"],
        "interval",
        seconds=15,
        id="usage_broadcast",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        jobs["approval_poll"],
        "interval",
        seconds=30,
        id="approval_poll",
        max_instances=1,
        coalesce=True,
    )

    if config.NLB_MONITOR_ENABLED:
        scheduler.add_job(
            jobs["nlb_health_monitor"],
            "interval",
            seconds=config.NLB_MONITOR_INTERVAL_SECONDS,
            id="nlb_health_monitor",
            max_instances=1,
            coalesce=True,
        )

    if config.STATUS_USAGE_RECORD_ENABLED:
        scheduler.add_job(
            jobs["usage_record_monitor"],
            "cron",
            hour=20,
            minute=0,
            timezone="Asia/Shanghai",
            id="usage_record_monitor",
            max_instances=1,
            coalesce=True,
        )

    if config.MODEL_GROUP_SPEND_ALERT_ENABLED:
        scheduler.add_job(
            jobs["model_group_spend_monitor"],
            "interval",
            minutes=config.MODEL_GROUP_SPEND_ALERT_INTERVAL_MINUTES,
            id="model_group_spend_monitor",
            max_instances=1,
            coalesce=True,
        )


def print_schedule(config):
    print("[Scheduler] Started:")
    print(f"  - Expiry check: every {config.KEY_CHECK_INTERVAL_MINUTES} min")
    print("  - Broadcast:    every 15 sec while clients are connected")
    print("  - Approval poll: every 30 sec")
    print(f"  - NLB monitor:  {'every ' + str(config.NLB_MONITOR_INTERVAL_SECONDS) + ' sec' if config.NLB_MONITOR_ENABLED else 'disabled'}")
    print(f"  - Usage record: {'daily at 20:00 Asia/Shanghai' if config.STATUS_USAGE_RECORD_ENABLED else 'disabled'}")
    print(f"  - Model group spend: {'every ' + str(config.MODEL_GROUP_SPEND_ALERT_INTERVAL_MINUTES) + ' min' if config.MODEL_GROUP_SPEND_ALERT_ENABLED else 'disabled'}")
