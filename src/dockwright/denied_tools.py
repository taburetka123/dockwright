DENIED_TOOLS = ("Artifact", "SendFeedback", "Workflow", "AskUserQuestion", "ReportFindings", "ScheduleWakeup")
MANAGER_KEEPS = ("ScheduleWakeup",)


def disallowed_tools_arg(manager: bool = False) -> str:
    names = [name for name in DENIED_TOOLS if not (manager and name in MANAGER_KEEPS)]
    return "--disallowedTools=" + ",".join(names)
