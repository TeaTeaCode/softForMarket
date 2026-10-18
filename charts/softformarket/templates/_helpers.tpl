{{- define "softformarket.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "softformarket.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "softformarket.labels" -}}
app.kubernetes.io/name: {{ include "softformarket.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "softformarket.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{ .Values.image.registry }}/{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}

{{- define "softformarket.secretName" -}}
{{ include "softformarket.fullname" . }}
{{- end -}}

{{- define "softformarket.configMapName" -}}
{{ include "softformarket.fullname" . }}-config
{{- end -}}

{{/* Том с несекретными YAML-конфигами из ConfigMap. Пусто, если configFiles выключен. */}}
{{- define "softformarket.configVolume" -}}
{{- if .Values.configFiles.enabled }}
- name: app-config
  configMap:
    name: {{ include "softformarket.configMapName" . }}
{{- end }}
{{- end -}}

{{/* Монтирование каждого конфиг-файла по отдельности (subPath), чтобы не
     перекрыть испечённые в образ соседние файлы в /app/config. */}}
{{- define "softformarket.configVolumeMounts" -}}
{{- if .Values.configFiles.enabled }}
{{- range .Values.configFiles.items }}
- name: app-config
  mountPath: {{ $.Values.configFiles.mountPath }}/{{ . }}
  subPath: {{ . }}
  readOnly: true
{{- end }}
{{- end }}
{{- end -}}

{{/* Несекретные config как env. Секреты идут из Secret через envFrom.secretRef. */}}
{{- define "softformarket.env" -}}
{{- range $k, $v := .Values.config }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}
