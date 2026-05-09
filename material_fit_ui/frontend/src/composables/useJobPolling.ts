import { onBeforeUnmount, ref } from 'vue';

export function useJobPolling() {
  const timer = ref<number | null>(null);

  function stopPolling(): void {
    if (timer.value != null) {
      window.clearInterval(timer.value);
      timer.value = null;
    }
  }

  function startPolling(callback: () => void, intervalMs = 1500): void {
    stopPolling();
    timer.value = window.setInterval(callback, intervalMs);
  }

  onBeforeUnmount(stopPolling);

  return { startPolling, stopPolling };
}
