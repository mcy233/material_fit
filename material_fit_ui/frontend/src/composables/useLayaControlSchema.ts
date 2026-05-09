import { computed, ref } from 'vue';
import type { LayaControlGroup } from '../types';

export function useLayaControlSchema() {
  const groups = ref<LayaControlGroup[]>([]);
  const searchableCount = computed(() =>
    groups.value.reduce((sum, group) => sum + (group.searchable_count ?? 0), 0),
  );

  return { groups, searchableCount };
}
