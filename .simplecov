# frozen_string_literal: true

# Bashcov executes the shell test harnesses too, but this report is specifically
# for the two user-facing entrypoints. Restricting the file set keeps test and
# temporary stub code out of the denominator.
SimpleCov.configure do
  coverage_dir "shell-coverage"
  cover "contrib/aib", "container/entrypoint.sh"
end
