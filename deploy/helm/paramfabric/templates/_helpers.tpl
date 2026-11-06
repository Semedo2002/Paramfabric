{{- define "paramfabric.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Secret-backed environment shared by API and worker pods. */}}
{{- define "paramfabric.secretEnv" -}}
- name: PARAMFABRIC_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.security.jwtSecretKey }}
- name: PARAMFABRIC_AUDIT_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.security.auditKeyKey }}
- name: PARAMFABRIC_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.security.databaseUrlKey }}
- name: PARAMFABRIC_LICENSE
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.security.licenseTokenKey }}
- name: PARAMFABRIC_LICENSE_PUBLIC_KEY
  value: /etc/paramfabric/license-public.pem
- name: PARAMFABRIC_SIGNING_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.security.signingKeyKey }}
      optional: true
- name: PARAMFABRIC_SIGNING_KEY_PASSPHRASE
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.security.signingKeyPassphraseKey }}
      optional: true
- name: PARAMFABRIC_WEBHOOK_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.security.webhookSecretKey }}
      optional: true
{{- if .Values.redis.enabled }}
- name: PARAMFABRIC_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.redis.urlKey }}
{{- end }}
{{- if .Values.objectStorage.s3.enabled }}
- name: PARAMFABRIC_S3_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.objectStorage.s3.accessKeyKey }}
- name: PARAMFABRIC_S3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.security.existingSecret }}
      key: {{ .Values.objectStorage.s3.secretKeyKey }}
{{- end }}
{{- end -}}

{{/* Volumes and mounts shared by API and worker pods. */}}
{{- define "paramfabric.volumeMounts" -}}
- name: state
  mountPath: /var/lib/paramfabric
- name: license
  mountPath: /etc/paramfabric/license-public.pem
  subPath: {{ .Values.security.licensePublicKeyKey }}
  readOnly: true
- name: config
  mountPath: /etc/paramfabric/params.yaml
  subPath: params.yaml
  readOnly: true
{{- end -}}

{{- define "paramfabric.volumes" -}}
- name: state
  {{- if .Values.persistence.enabled }}
  persistentVolumeClaim:
    claimName: {{ include "paramfabric.fullname" . }}-state
  {{- else }}
  emptyDir: {}
  {{- end }}
- name: license
  secret:
    secretName: {{ .Values.security.existingSecret }}
- name: config
  configMap:
    name: {{ include "paramfabric.fullname" . }}-fabric
{{- end -}}
