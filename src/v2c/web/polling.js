/** Schedule after completion so slow refreshes never accumulate overlapping polls. */
export function createPoller(refresh, onError, interval = 5000) {
  let timer = null;
  let generation = 0;

  function stop() {
    generation += 1;
    clearTimeout(timer);
    timer = null;
  }

  function start() {
    stop();
    const activeGeneration = generation;
    async function tick() {
      try {
        await refresh();
      } catch (error) {
        if (activeGeneration === generation) onError(error);
      } finally {
        if (activeGeneration === generation) timer = setTimeout(tick, interval);
      }
    }
    timer = setTimeout(tick, interval);
  }

  return { start, stop };
}
