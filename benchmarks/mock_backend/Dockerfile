# =============================================================================
# A backend that does no work.
#
# That is the whole point. To measure what the gateway costs, the thing behind
# it must contribute as close to zero as possible -- otherwise the backend's
# own variance swamps the difference you are trying to see.
# =============================================================================
FROM nginx:1.27-alpine

ENV INSTANCE_NAME=mock
COPY default.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 8080
