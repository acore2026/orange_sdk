package com.rayneo.agent.example

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.*
import org.junit.Test

class PatrolDispatchTest {
    private fun reply(task: String, type: String = "patrol_accepted", duplicate: Boolean = false,
                      command: String = "patrol_forward") = buildJsonObject {
        put("task_id", task)
        put("type", type)
        put("duplicate", duplicate)
        put("command", command)
        put("reason", "failed")
    }

    @Test fun newRoundsInSameGroupHaveDifferentIds() {
        val first = PatrolDispatch("group", "dog")
        val next = PatrolDispatch("group", "dog")
        assertNotEquals(first.taskId, next.taskId)
        val retryId = first.taskId
        assertEquals(retryId, first.taskId)
    }

    @Test fun earlyAckIsRetainedBeforeSenderReturns() = runBlocking {
        val d = PatrolDispatch("g", "d")
        assertTrue(d.receive("g", "d", reply(d.taskId)))
        assertNull(d.acceptance.await())
        assertFalse(d.entryCompletion.isCompleted)
        d.receive("g", "d", reply(d.taskId, "dog_motion_completed"))
        assertNull(d.entryCompletion.await())
    }

    @Test fun duplicateDoesNotAuthorizeNewMotionOrSuccess() = runBlocking {
        val d = PatrolDispatch("g", "d")
        d.receive("g", "d", reply(d.taskId, duplicate = true))
        assertTrue(d.acceptance.await()!!.contains("Duplicate"))
        assertFalse(d.entryCompletion.isCompleted)
    }

    @Test fun retryDuplicateCannotReplaceAlreadyObservedAcceptance() = runBlocking {
        val d = PatrolDispatch("g", "d")
        d.receive("g", "d", reply(d.taskId))
        d.receive("g", "d", reply(d.taskId, duplicate = true))
        assertNull(d.acceptance.await())
        assertFalse(d.entryCompletion.isCompleted)
    }

    @Test fun wrongGroupSenderTaskAndManualCompletionAreIgnored() {
        val d = PatrolDispatch("g", "d")
        assertFalse(d.receive("other", "d", reply(d.taskId)))
        assertFalse(d.receive("g", "other", reply(d.taskId)))
        assertFalse(d.receive("g", "d", reply("old")))
        assertFalse(d.receive("g", "d", reply(d.taskId, "dog_motion_completed", command = "forward")))
        assertFalse(d.acceptance.isCompleted)
        assertFalse(d.entryCompletion.isCompleted)
    }

    @Test fun rejectionAndEntryFailureRemainFailures() = runBlocking {
        val rejected = PatrolDispatch("g", "d")
        rejected.receive("g", "d", reply(rejected.taskId, "patrol_rejected"))
        assertNotNull(rejected.acceptance.await())
        val failed = PatrolDispatch("g", "d")
        failed.receive("g", "d", reply(failed.taskId))
        failed.receive("g", "d", reply(failed.taskId, "dog_motion_failed"))
        assertNull(failed.acceptance.await())
        assertEquals("failed", failed.entryCompletion.await())
    }
}
