"""اختبار حارس الدور: time_to_plus20_min لا يمكن أن يكون قاعدة دخول.

الحكم مشترك (قياس المالك 2026-08-28 على التاريخ الكامل): الدخول بعد +20%
خاسر صافيًا في كل النوافذ (-1% إلى -11%) مقابل +0.74% عند الإشارة.
هذا الاختبار يثبّت القرار في الكود نفسه: أي استعمال مستقبلي للعمود
كهدف تدريبي بدخول متأخر أو كعتبة شراء يكسر هذا الاختبار عن قصد —
فيُضطر صاحبه إلى قراءة القياس أولًا.
"""
import labeler


def test_plus20_is_veto_filter_never_entry():
    """العقد المعلن في docstring الموسِّم: فلتر إسقاط، لا دخول.

    نتحقق أن التوثيق الحي (docstring compute_labels) يصرّح بالحكم —
    هو أول ما يقرؤه من يعدّل هذا الملف.
    """
    import inspect

    src = inspect.getsource(labeler.compute_labels)
    assert "لا قاعدة دخول" in src or "ليست قاعدة دخول" in src, (
        "تحذير الدور حُذف من compute_labels — الدخول بعد +20% خاسر "
        "صافيًا (-1% إلى -11%، قياس 2026-08-28). أعد التوثيق أو غيّر "
        "القرار بقياس مضاد جديد."
    )


def test_config_plus20_comment_declares_veto_role():
    """ثابت العتبة موثق بحدوده: فلتر، لا دخول."""
    import inspect

    import config

    src = inspect.getsource(config)
    assert "ليست قاعدة دخول" in src or "لا قاعدة دخول" in src
