resource "aws_route53_zone" "marketplace_team" {
  name = "marketplace.team"
}

resource "aws_route53_record" "preview_ns" {
  zone_id = "${aws_route53_zone.marketplace_team.zone_id}"
  name    = "preview.marketplace.team"
  type    = "NS"
  ttl     = "3600"

  records = [
    "ns-389.awsdns-48.com",
    "ns-2018.awsdns-60.co.uk",
    "ns-1176.awsdns-19.org",
    "ns-530.awsdns-02.net",
  ]
}

resource "aws_route53_record" "staging_ns" {
  zone_id = "${aws_route53_zone.marketplace_team.zone_id}"
  name    = "staging.marketplace.team"
  type    = "NS"
  ttl     = "3600"

  records = [
    "ns-175.awsdns-21.com",
    "ns-781.awsdns-33.net",
    "ns-1457.awsdns-54.org",
    "ns-1828.awsdns-36.co.uk",
  ]
}

# Certificate for Jenkins ELB

resource "aws_acm_certificate" "jenkins_wildcard_elb_certificate" {
  domain_name       = "*.marketplace.team"
  validation_method = "DNS"

  tags {
    Name = "Jenkins ELB certificate"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "jenkins_wildcard_elb_cert_validation" {
  name    = "${aws_acm_certificate.jenkins_wildcard_elb_certificate.domain_validation_options.0.resource_record_name}"
  type    = "${aws_acm_certificate.jenkins_wildcard_elb_certificate.domain_validation_options.0.resource_record_type}"
  zone_id = "${aws_route53_zone.marketplace_team.id}"
  records = ["${aws_acm_certificate.jenkins_wildcard_elb_certificate.domain_validation_options.0.resource_record_value}"]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "jenkins_wildcard_elb_certificate" {
  certificate_arn         = "${aws_acm_certificate.jenkins_wildcard_elb_certificate.arn}"
  validation_record_fqdns = ["${aws_route53_record.jenkins_wildcard_elb_cert_validation.fqdn}"]
}
