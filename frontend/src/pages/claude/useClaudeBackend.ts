import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'

export const CLAUDE_BACKENDS = [
  { id: 'claude', label: 'Claude', detail: 'Suscripción de Anthropic. Hace falta el token del CLI.' },
  {
    id: 'deepseek-v4-flash',
    label: 'DeepSeek V4 Flash',
    detail: 'Modelo del clúster. No hay sesión que pegar.',
  },
  {
    id: 'glm5.3-flash',
    label: 'GLM 5.3 Flash',
    detail: 'Modelo del clúster, más contexto para una review entera.',
  },
] as const

export type ClaudeBackendId = (typeof CLAUDE_BACKENDS)[number]['id']

export interface ClaudeBackendState {
  choice: ClaudeBackendId | null
  effective: ClaudeBackendId
  needs_session: boolean
}

const QUERY_KEY = ['claude-backend'] as const

export function useClaudeBackend() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => api.get<ClaudeBackendState>('/api/claude/backend'),
  })
}

export function useSaveClaudeBackend() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (choice: ClaudeBackendId) =>
      api.post<ClaudeBackendState>('/api/claude/backend', { choice }),
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: QUERY_KEY })
    },
    onSuccess: (state) => {
      queryClient.setQueryData(QUERY_KEY, state)
    },
  })
}
