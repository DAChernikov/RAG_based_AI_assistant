{{- define "rag.name" -}}rag-assistant{{- end -}}
{{- define "rag.labels" -}}
app.kubernetes.io/name: {{ include "rag.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
