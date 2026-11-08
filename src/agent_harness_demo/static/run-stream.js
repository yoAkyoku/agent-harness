// One observer owns a contiguous cursor; retries never submit a new task.
export function observeEvents(runId, {onEvent, onState, onError}) {
  let socket = null, timer = null, stopped = false, cursor = 0, attempts = 0;
  function state(value) { onState(value, cursor); }
  function connect() {
    if (stopped) return;
    state(attempts ? "正在補讀保存事件" : "正在連線");
    let gap = false;
    const current = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/runs/${runId}/stream?after=${cursor}&flow=ack`);
    socket = current;
    current.onopen = () => { if (!stopped && socket === current) state("已連線"); };
    current.onmessage = ({data}) => {
      if (stopped || socket !== current) return;
      try {
        const event = JSON.parse(data);
        if (event.run_id !== runId || !Number.isSafeInteger(event.sequence) || event.sequence < 1) throw new Error("事件格式無法辨識，請重新載入紀錄。");
        if (event.sequence > cursor + 1) { gap = true; current.close(); return; }
        if (event.sequence === cursor + 1) {
          onEvent(event); cursor = event.sequence; attempts = 0; state("已連線");
        }
        current.send(JSON.stringify({ack: cursor}));
      } catch (error) { stopped = true; current.close(); state("更新已停止"); onError(error); }
    };
    current.onclose = ({code}) => {
      if (stopped || socket !== current) return;
      if (!gap && code === 1000) { state("保存事件已同步"); return; }
      if (code === 4400 || code === 4404) { state("請重新載入執行紀錄"); return; }
      attempts += 1;
      if (attempts > 5) { state("連線中斷，可手動重新連線"); return; }
      state("連線中斷，正在重連");
      timer = setTimeout(connect, Math.min(250 * 2 ** (attempts - 1), 4000));
    };
    current.onerror = () => { if (!stopped && socket === current) state("連線暫時不可用"); };
  }
  connect();
  return {
    close() { stopped = true; clearTimeout(timer); if (socket) socket.close(); },
    reconnect() { if (stopped) return; clearTimeout(timer); attempts = 0; const previous = socket; socket = null; if (previous) previous.close(); connect(); },
  };
}
