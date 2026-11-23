{{- define "kaji-agent.secret" -}}
{{- default (printf "%s-token" .Release.Name) .Values.existingSecret -}}
{{- end -}}
